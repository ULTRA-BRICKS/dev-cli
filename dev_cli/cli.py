"""``dev`` CLI - safe project-scoped development commands.

Accessed via: the ``dev`` console script (dev-cli). ``dev run`` executes a named top-level
``rig.yaml`` script or e2e job from the repo root; ``dev start/list/stop`` manages
the configured dev server and e2e jobs. ``dev stop`` terminates only a process that first
passes both checks: it looks like a development/e2e tool, and its cwd or command path is
scoped to the repo (or to explicit ``DEV_PROJECT_PATHS`` roots).

Assumptions:
    * Top-level imports are stdlib-only. ``agenttools_config`` (vendored under ``vendor/``)
      and PyYAML are imported only inside ``_load_rig_config`` when a command actually reads
      rig.yaml.
    * SIGTERM is sent only after validation; process and signal helpers are small
      functions so tests can monkeypatch them without touching real processes.
    * ``dev-cli`` was spun out of the agent-tools umbrella (it lived at
      ``lib/agenttools_dev``). The ``--agenttools-dev-probe`` flag and the ``.agenttools-dev``
      state-dir name are preserved as external wire contracts (agent-tools ship.sh + the
      portable git-hooks probe them); do NOT rename them.
    * ``dev kill-orphan`` / ``dev clean-logs`` never accept a free-text pattern. Both are
      validated against ``rules.yaml`` via ``dev_cli.cleanup_rules`` (structured
      ``--harness``/``--worker``/``--instance`` fields, or an explicit path checked against an
      allowed root+suffix) — see that module's docstring. This is a separate validation path
      from ``dev stop``'s ``_validate_stop_target``; never fold the two together.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from . import cleanup_rules

EXIT_USAGE = 2
EXIT_MISSING_DEP = 127
DEV_PROJECT_PATHS_ENV = "DEV_PROJECT_PATHS"


class DevError(RuntimeError):
    """A user-actionable CLI error with a stable exit code."""

    def __init__(self, message: str, exit_code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _build_parser() -> argparse.ArgumentParser:
    common_epilog = (
        "Exit codes: 0 success; 'dev run' returns the command's exit code; "
        "2 means invalid args/config or an unsafe stop target; 127 means a required "
        "platform helper is missing.\n\n"
        f"Env: {DEV_PROJECT_PATHS_ENV} is an os.pathsep-separated list of extra project "
        "roots allowed for this session. Agents should set it only when the user "
        "explicitly asks to work across multiple projects."
    )
    parser = argparse.ArgumentParser(
        prog="dev",
        description="Run rig.yaml scripts and manage project-scoped dev/e2e processes.",
        epilog=common_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start",
        help="start a configured dev server or e2e job in the background",
        epilog=(
            "Targets come from rig.yaml dev.server or dev.e2e jobs. Commands must look like "
            "development/e2e tools; obvious destructive heads are refused.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    start.add_argument(
        "target", help="configured target name: server, e2e, or a dev.e2e.jobs entry"
    )
    start.add_argument("target_args", nargs=argparse.REMAINDER, metavar="args")

    list_cmd = sub.add_parser(
        "list",
        help="list configured and running dev/e2e targets for this repo",
        epilog=common_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    list_cmd.set_defaults(_list=True)

    status = sub.add_parser(
        "status",
        help="show status/progress for configured dev/e2e targets",
        epilog=(
            "Status reads configured target metadata and local state files. E2e artifacts "
            "can declare artifacts_root and logs_root.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status.add_argument("target", nargs="?", help="configured target name")

    logs = sub.add_parser(
        "logs",
        help="print a configured target log, usually from the latest e2e run directory",
        epilog=common_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    logs.add_argument("target", help="configured target name")
    logs.add_argument(
        "--tail", type=_positive_int, default=200, help="number of log lines to print"
    )

    run = sub.add_parser(
        "run",
        help="run a named top-level rig.yaml script or dev.e2e job from the repo root",
        epilog=(
            "Script values may be a string command or a mapping with cmd:. Extra args "
            "after '--' are shell-quoted and appended to the command. Top-level scripts "
            "win; if absent, dev.e2e or dev.e2e.jobs.<name> can supply an e2e job. Missing names or "
            "invalid config exit 2. --repo-only ignores the machine-wide rig config.\n\n"
            + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run.add_argument(
        "--repo-only",
        action="store_true",
        help="ignore the global rig config and run only from this repo's rig.yaml",
    )
    run.add_argument("script", help="script name under top-level rig.yaml scripts:")
    run.add_argument("script_args", nargs=argparse.REMAINDER, metavar="args")

    has_script = sub.add_parser(
        "has-script",
        help="check whether top-level rig.yaml scripts:<name> exists",
        epilog=(
            "Used by portable shell hooks before they choose `dev run --repo-only test`; prints nothing. "
            "Exit 0 means the script exists, 1 means absent, 2/127 means config/dependency error. "
            "--repo-only ignores the machine-wide rig config so hooks only react to committed "
            "repo scripts.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    has_script.add_argument(
        "--repo-only",
        action="store_true",
        help="ignore the global rig config and check only this repo's rig.yaml",
    )
    has_script.add_argument("script", help="script name under top-level rig.yaml scripts:")

    stop = sub.add_parser(
        "stop",
        help="terminate a validated dev/e2e process by target, pid, port, or pgid",
        epilog=(
            f"Before SIGTERM, the target must be a known dev/e2e runner and scoped to the "
            f"current repo or {DEV_PROJECT_PATHS_ENV}; --pgid validates every visible "
            "process group member first.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    stop.add_argument(
        "target", nargs="?", help="configured target name: server, e2e, or a dev.e2e.jobs entry"
    )
    target = stop.add_mutually_exclusive_group()
    target.add_argument("--pid", type=_positive_int, help="process id to validate and stop")
    target.add_argument("--port", type=_port_int, help="listening TCP port to resolve and stop")
    target.add_argument("--pgid", type=_positive_int, help="process group id to validate and stop")

    e2e = sub.add_parser(
        "e2e",
        help="first-class e2e run/status/logs/stop aliases",
        epilog=common_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    e2e_sub = e2e.add_subparsers(dest="e2e_command", required=True)
    e2e_run = e2e_sub.add_parser("run", help="run a configured e2e job in the foreground")
    e2e_run.add_argument("target")
    e2e_run.add_argument("target_args", nargs=argparse.REMAINDER, metavar="args")
    e2e_status = e2e_sub.add_parser("status", help="show status/progress for an e2e job")
    e2e_status.add_argument("target")
    e2e_logs = e2e_sub.add_parser("logs", help="print an e2e job log")
    e2e_logs.add_argument("target")
    e2e_logs.add_argument("--tail", type=_positive_int, default=200)
    e2e_stop = e2e_sub.add_parser("stop", help="stop a configured e2e job")
    e2e_stop.add_argument("target")

    shot = sub.add_parser(
        "shot",
        help="capture a screenshot, check it is not blank, and record the proof",
        epilog=(
            "Captures with a scroll sweep so reveal-on-scroll sections have entered the "
            "viewport before the shutter, measures the result, and writes a proof record ONLY "
            "if the capture is admissible. The record is bound to the capture bytes and to "
            "`git diff --cached`, so it cannot be reused for a different change or survive "
            "further edits. The agent never writes the record; this command does.\n\n"
            "Exit codes: 0 captured and attested; 3 the capture is degenerate (blank band, "
            "wrong viewport) and no proof was written; 4 the capture is fine but proof could "
            "not be recorded (nothing staged, or the record could not be written) - retaking "
            "will not help; 127 agent-browser is missing.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    shot.add_argument("url", help="URL to capture, e.g. http://localhost:4173/")
    shot.add_argument("--out", required=True, help="path to write the PNG capture to")
    shot.add_argument(
        "--viewport",
        default="1440x900",
        help="viewport as WIDTHxHEIGHT (default: 1440x900)",
    )
    shot.add_argument(
        "--repo",
        default=".",
        help="repository the proof is bound to (default: current directory)",
    )
    shot.add_argument(
        "--sweep-step",
        type=_positive_int,
        default=400,
        help="pixels per scroll step during the sweep (default: 400)",
    )
    shot.add_argument(
        "--no-attest",
        action="store_true",
        help="capture and measure only; do not write a proof record",
    )
    shot.add_argument("--json", action="store_true", help="print the result as JSON")

    env = sub.add_parser(
        "env",
        help=f"print shell exports for {DEV_PROJECT_PATHS_ENV}",
        epilog=(
            "This prints an export line only; it cannot mutate the parent shell. "
            "Use it for explicit multi-project sessions.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    env.add_argument(
        "--add-project", required=True, help="project root to append to the session env"
    )

    kill_orphan = sub.add_parser(
        "kill-orphan",
        help="kill orphaned harness processes matched against a named family in rules.yaml",
        epilog=(
            "Targets are structured (--harness/--worker/--instance), never a free-text "
            "pattern: the family in rules.yaml supplies the argv anchor, id shape, and signal "
            "method, so an unscoped kill cannot be expressed through this command. Editing "
            "rules.yaml requires a passed review-cli quorum (see "
            "scripts/git-hooks/check_rules_quorum.py).\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    kill_orphan.add_argument(
        "--harness", required=True, help="kill family name from rules.yaml, e.g. vscode-e2e-harness"
    )
    kill_orphan.add_argument("--worker", help="worker/slot identifier required by most families")
    kill_orphan.add_argument(
        "--instance", help="optional unique instance id; omit to target the whole worker slot"
    )

    clean_logs = sub.add_parser(
        "clean-logs",
        help="delete explicit debug/run log files after validating them against rules.yaml",
        epilog=(
            "Each path must resolve (after following symlinks) under an allowed root with an "
            "allowed suffix in rules.yaml clean_log_rules; globs are refused, only explicit "
            "file paths. Every path in one call is validated before any file is "
            "deleted.\n\n" + common_epilog
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    clean_logs.add_argument("paths", nargs="+", help="explicit log file path(s) to delete")

    return parser


def _positive_int(raw: str) -> int:
    try:
        value = int(raw, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not an integer") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _port_int(raw: str) -> int:
    value = _positive_int(raw)
    if value > 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    # Backward-compat wire contract: agent-tools ship.sh + the portable git-hooks probe a
    # PATH `dev` with this hidden flag and expect the exact "agenttools-dev" token. Keep it
    # stable across the extraction so an installed dev from dev-cli still answers the probe.
    if raw_args == ["--agenttools-dev-probe"]:
        print("agenttools-dev")
        return 0
    if raw_args and raw_args[0] in ("--version", "-V", "version"):
        from . import __version__

        print(f"dev {__version__}")
        return 0
    if raw_args and raw_args[0] == "install-skill":
        if len(raw_args) > 1 and raw_args[1] in ("-h", "--help"):
            print(
                "Usage: dev install-skill\n\nRegister the dev agent skill with detected harnesses (idempotent)."
            )
            return 0
        from .install import install_skill

        return install_skill()
    parser = _build_parser()
    args = parser.parse_args(raw_args)
    try:
        if args.command == "start":
            return _cmd_start(args)
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "logs":
            return _cmd_logs(args)
        if args.command == "run":
            return _cmd_run(args)
        if args.command == "has-script":
            return _cmd_has_script(args)
        if args.command == "stop":
            return _cmd_stop(args)
        if args.command == "e2e":
            return _cmd_e2e(args)
        if args.command == "shot":
            return _cmd_shot(args)
        if args.command == "env":
            return _cmd_env(args)
        if args.command == "kill-orphan":
            return _cmd_kill_orphan(args)
        if args.command == "clean-logs":
            return _cmd_clean_logs(args)
    except DevError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    parser.print_help()
    return EXIT_USAGE


def _cmd_run(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = (
        _load_rig_config(repo_root, include_global=False)
        if args.repo_only
        else _load_rig_config(repo_root)
    )
    command = _script_command(config, args.script, _strip_arg_separator(args.script_args))
    _ensure_lifecycle_command(command, repo_root)
    return _run_shell(command, repo_root)


def _cmd_has_script(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = (
        _load_rig_config(repo_root, include_global=False)
        if args.repo_only
        else _load_rig_config(repo_root)
    )
    scripts = config.get("scripts")
    if scripts is None:
        return 1
    if not isinstance(scripts, dict):
        raise DevError("rig.yaml top-level scripts: must be a mapping of script names to commands")
    return 0 if args.script in scripts else 1


def _cmd_start(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = _load_rig_config(repo_root)
    target = _configured_target(config, args.target)
    command = _with_extra_args(_target_command(target), _strip_arg_separator(args.target_args))
    _ensure_lifecycle_command(command, repo_root)
    pid = _start_process(command, repo_root, _start_log_path(repo_root, target))
    record = {
        "kind": target["kind"],
        "name": target["name"],
        "pid": pid,
        "pgid": pid,
        "command": command,
        "cwd": str(repo_root),
    }
    if target.get("ports"):
        record["ports"] = target["ports"]
        record["port"] = target["ports"][0]
    _write_state(repo_root, record)
    print(f"started {target['kind']} {target['name']} pid {pid}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = _load_rig_config(repo_root)
    configured = {
        (target["kind"], target["name"]): target for target in _configured_targets(config)
    }
    seen: set[tuple[str, str]] = set()
    for record, _path in _read_states(repo_root):
        key = (str(record.get("kind", "target")), str(record.get("name", "")))
        seen.add(key)
        pid = int(record.get("pid", 0) or 0)
        status = "running" if pid > 0 and _pid_alive(pid) else "stale"
        ports = _ports_text(
            record.get("ports") or ([record["port"]] if record.get("port") is not None else [])
        )
        command = f" cmd={record.get('command', '')}" if record.get("command") else ""
        print(f"{key[0]} {key[1]} {status} pid={pid}{ports}{command}")
    for key, target in sorted(configured.items()):
        if key not in seen:
            ports = _ports_text(target.get("ports") or [])
            command = f" cmd={target['command']}" if target.get("command") else ""
            print(f"{target['kind']} {target['name']} configured{ports}{command}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = _load_rig_config(repo_root)
    if args.target:
        target = _configured_target(config, args.target)
        print(_status_line(repo_root, target))
        return 0
    for target in _configured_targets(config):
        print(_status_line(repo_root, target))
    return 0


def _cmd_logs(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = _load_rig_config(repo_root)
    target = _configured_target(config, args.target)
    return _print_target_log(repo_root, target, args.tail)


def _print_target_log(repo_root: Path, target: dict, tail: int) -> int:
    log_path = _target_log_path(repo_root, target)
    if log_path is None:
        raise DevError(
            f"dev target {target['name']} has no readable log under logs_root in rig.yaml"
        )
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise DevError(f"cannot read log for {target['name']}: {log_path}: {exc}") from exc
    for line in lines[-tail:]:
        print(line)
    return 0


def _cmd_e2e(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    config = _load_rig_config(repo_root)
    target = _configured_e2e_target(config, args.target)
    if args.e2e_command == "run":
        command = _with_extra_args(_target_command(target), _strip_arg_separator(args.target_args))
        _ensure_lifecycle_command(command, repo_root)
        return _run_shell(command, repo_root)
    if args.e2e_command == "status":
        print(_status_line(repo_root, target))
        return 0
    if args.e2e_command == "logs":
        return _print_target_log(repo_root, target, args.tail)
    if args.e2e_command == "stop":
        return _stop_target(repo_root, config, target)
    raise DevError(f"unknown e2e command: {args.e2e_command}")


def _strip_arg_separator(args: Sequence[str]) -> List[str]:
    items = list(args)
    if items and items[0] == "--":
        return items[1:]
    return items


def _find_repo_root(start: Path) -> Path:
    start = Path(start).resolve()
    git = shutil.which("git")
    if git:
        result = subprocess.run(
            [git, "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
    for candidate in (start, *start.parents):
        if (candidate / "rig.yaml").is_file() or (candidate / ".git").exists():
            return candidate.resolve()
    raise DevError(
        "cannot locate a repo root; run from inside a git checkout or a directory with rig.yaml"
    )


def _load_rig_config(repo_root: Path, *, include_global: bool = True) -> dict:
    try:
        from agenttools_config import ConfigError, load_config
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            raise DevError(
                "cannot read rig.yaml because PyYAML is not installed; install the dev "
                "package with its runtime dependencies",
                EXIT_MISSING_DEP,
            ) from exc
        raise DevError(
            "cannot read rig.yaml because agenttools-config is not installed; "
            "install the dev package with its runtime dependencies",
            EXIT_MISSING_DEP,
        ) from exc
    try:
        return load_config(tool="rig", repo_root=repo_root, include_global=include_global).data
    except ModuleNotFoundError as exc:
        if exc.name == "yaml":
            raise DevError(
                "cannot read rig.yaml because PyYAML is not installed; install the dev "
                "package with its runtime dependencies",
                EXIT_MISSING_DEP,
            ) from exc
        raise
    except ConfigError as exc:
        raise DevError(f"invalid rig.yaml/config cascade: {exc}") from exc


def _script_command(config: dict, script: str, extra_args: Sequence[str]) -> str:
    scripts = config.get("scripts")
    if scripts is not None and not isinstance(scripts, dict):
        raise DevError("rig.yaml top-level scripts: must be a mapping of script names to commands")
    if isinstance(scripts, dict) and script in scripts:
        return _with_extra_args(
            _command_from_entry(scripts[script], f"scripts.{script}"),
            extra_args,
        )
    e2e_targets = {
        target["name"]: target for target in _configured_targets(config) if target["kind"] == "e2e"
    }
    if script in e2e_targets:
        command = _target_command(e2e_targets[script])
        return _with_extra_args(command, extra_args)
    known_scripts = sorted(str(name) for name in scripts) if isinstance(scripts, dict) else []
    known_e2e = sorted(str(name) for name in e2e_targets)
    known = (
        ", ".join(
            [
                *known_scripts,
                *[("dev.e2e" if name == "e2e" else f"dev.e2e.jobs.{name}") for name in known_e2e],
            ]
        )
        or "(none)"
    )
    if scripts is None and not known_e2e:
        raise DevError(
            "rig.yaml does not define top-level scripts: or dev.e2e jobs. Add, for example:\n"
            "  scripts:\n"
            "    test: <command>"
        )
    raise DevError(
        f"script {script!r} is not defined in rig.yaml scripts/dev.e2e. "
        f"Known names: {known}. Add scripts.{script}, dev.e2e.jobs.{script}.script, or choose an existing name."
    )


def _with_extra_args(command: str, extra_args: Sequence[str]) -> str:
    if extra_args:
        return " ".join([command, *[shlex.quote(arg) for arg in extra_args]])
    return command


def _command_from_entry(value: object, label: str) -> str:
    if isinstance(value, str):
        command = value.strip()
    elif isinstance(value, dict):
        cmd = value.get("cmd")
        command = cmd.strip() if isinstance(cmd, str) else ""
    else:
        command = ""
    if not command:
        raise DevError(f"{label} must be a non-empty string command or a mapping with cmd:")
    return command


def _dev_section(config: dict, section: str) -> dict:
    dev = config.get("dev") or {}
    if not isinstance(dev, dict):
        raise DevError("rig.yaml dev: must be a mapping")
    value = dev.get(section) or {}
    if not isinstance(value, dict):
        raise DevError(f"rig.yaml dev.{section}: must be a mapping")
    return value


def _configured_targets(config: dict) -> List[dict]:
    targets: List[dict] = []
    server = _dev_section(config, "server")
    if server:
        targets.append(_server_target(config, server))

    e2e = _dev_section(config, "e2e")
    if e2e:
        if e2e.get("script") is not None:
            targets.append(_e2e_target_from_entry(config, "e2e", e2e, "dev.e2e", parent=e2e))
        jobs = e2e.get("jobs") or {}
        if not isinstance(jobs, dict):
            raise DevError("rig.yaml dev.e2e.jobs: must be a mapping")
        for name, entry in jobs.items():
            if not isinstance(entry, dict):
                raise DevError(f"dev.e2e.jobs.{name} must be a mapping")
            targets.append(
                _e2e_target_from_entry(config, str(name), entry, f"dev.e2e.jobs.{name}", parent=e2e)
            )
    return targets


def _server_target(config: dict, entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise DevError("rig.yaml dev.server: must be a mapping")
    target = {
        "kind": "server",
        "name": "server",
        "command": _command_from_script_ref(
            config, entry.get("script"), "dev.server.script", required=False
        ),
        "ports": _entry_ports(entry, "dev.server"),
    }
    for key in ("url", "ready_url", "logs_root"):
        if isinstance(entry.get(key), str) and entry[key].strip():
            target[key] = entry[key].strip()
    if isinstance(entry.get("process_matchers"), list):
        target["process_matchers"] = [
            item for item in entry["process_matchers"] if isinstance(item, str)
        ]
    return target


def _e2e_target_from_entry(
    config: dict, name: str, entry: dict, label: str, *, parent: dict
) -> dict:
    target = {
        "kind": "e2e",
        "name": name,
        "command": _command_from_script_ref(
            config, entry.get("script"), f"{label}.script", required=False
        ),
    }
    for key in ("requires_server", "artifacts_root", "logs_root"):
        value = entry.get(key, parent.get(key))
        if value is not None:
            target[key] = value
    return target


def _command_from_script_ref(config: dict, script: object, label: str, *, required: bool) -> str:
    if script is None:
        if required:
            raise DevError(f"{label} is required for this dev target")
        return ""
    if not isinstance(script, str) or not script.strip():
        raise DevError(f"{label} must be a non-empty script name")
    scripts = config.get("scripts")
    if not isinstance(scripts, dict) or script not in scripts:
        known = (
            ", ".join(sorted(str(name) for name in scripts))
            if isinstance(scripts, dict)
            else "(none)"
        )
        raise DevError(f"{label} references missing scripts.{script}; known scripts: {known}")
    return _command_from_entry(scripts[script], f"scripts.{script}")


def _target_command(target: dict) -> str:
    command = target.get("command")
    if isinstance(command, str) and command.strip():
        return command
    raise DevError(f"dev target {target['name']!r} has no script configured")


def _configured_target(config: dict, name: str) -> dict:
    targets = _configured_targets(config)
    for target in targets:
        if target["name"] == name:
            return target
    known = ", ".join(f"{target['kind']}:{target['name']}" for target in targets) or "(none)"
    raise DevError(
        f"dev target {name!r} is not defined in rig.yaml dev.server/dev.e2e. "
        f"Known targets: {known}."
    )


def _configured_e2e_target(config: dict, name: str) -> dict:
    for target in _configured_targets(config):
        if target["kind"] == "e2e" and target["name"] == name:
            return target
    known = (
        ", ".join(
            sorted(
                target["name"] for target in _configured_targets(config) if target["kind"] == "e2e"
            )
        )
        or "(none)"
    )
    raise DevError(f"dev.e2e target {name!r} is not defined. Known e2e targets: {known}.")


def _entry_ports(entry: object, label: str) -> List[int]:
    if not isinstance(entry, dict):
        return []
    raw = entry.get("ports")
    if raw is None and entry.get("port") is not None:
        raw = [entry["port"]]
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DevError(f"{label}.ports must be a list of TCP ports")
    ports: List[int] = []
    for item in raw:
        if isinstance(item, bool):
            raise DevError(f"{label}.ports must contain integer TCP ports")
        try:
            port = int(item)
        except (TypeError, ValueError) as exc:
            raise DevError(f"{label}.ports must contain integer TCP ports") from exc
        if port <= 0 or port > 65535:
            raise DevError(f"{label}.ports entries must be between 1 and 65535")
        ports.append(port)
    return ports


def _ports_text(ports: Sequence[object]) -> str:
    items = [str(port) for port in ports if port is not None]
    return f" ports={','.join(items)}" if items else ""


def _status_line(repo_root: Path, target: dict) -> str:
    record, _path = _state_for_target(repo_root, target["kind"], target["name"])
    if record is not None:
        pid = int(record.get("pid", 0) or 0)
        state = "running" if pid > 0 and _pid_alive(pid) else "stale"
        parts = [f"{target['kind']} {target['name']} {state} pid={pid}"]
        ports = record.get("ports") or ([record["port"]] if record.get("port") is not None else [])
        if ports:
            parts.append(f"ports={','.join(str(port) for port in ports)}")
    else:
        parts = [f"{target['kind']} {target['name']} configured"]
        if target.get("ports"):
            parts.append(f"ports={','.join(str(port) for port in target['ports'])}")
    artifacts = _target_artifacts(repo_root, target)
    if artifacts.get("latest_run") is not None:
        parts.append(f"latest_run={artifacts['latest_run']}")
    if artifacts.get("exit_code") is not None:
        parts.append(f"exit_code={artifacts['exit_code']}")
    if artifacts.get("log_name") is not None:
        parts.append(f"log={artifacts['log_name']}")
    return " ".join(parts)


def _target_artifacts(repo_root: Path, target: dict) -> dict:
    status_cfg = target.get("status") if isinstance(target.get("status"), dict) else {}
    latest_run = _latest_run_dir(repo_root, status_cfg)
    if latest_run is None and isinstance(target.get("artifacts_root"), str):
        latest_run = _latest_child_dir(repo_root, target["artifacts_root"], "artifacts_root")
    log_path = _target_log_path(repo_root, target, latest_run=latest_run)
    exit_code = None
    exit_code_path = _status_path(repo_root, status_cfg, "exit_code", latest_run)
    if exit_code_path is None and latest_run is not None:
        candidate = latest_run / "exit-code"
        if candidate.is_file():
            exit_code_path = _project_scoped_path(repo_root, candidate, "artifacts_root.exit-code")
    if exit_code_path is not None and exit_code_path.is_file():
        try:
            exit_code = exit_code_path.read_text(encoding="utf-8").strip()
        except OSError:
            exit_code = None
    return {
        "latest_run": latest_run,
        "log_path": log_path,
        "log_name": str(log_path) if log_path is not None else None,
        "exit_code": exit_code,
    }


def _target_log_path(
    repo_root: Path, target: dict, *, latest_run: Optional[Path] = None
) -> Optional[Path]:
    status_cfg = target.get("status") if isinstance(target.get("status"), dict) else {}
    artifacts_root_path = None
    if isinstance(target.get("artifacts_root"), str):
        artifacts_root_path = _project_scoped_config_path(
            repo_root, target["artifacts_root"], "artifacts_root"
        )
    if latest_run is None:
        latest_run = _latest_run_dir(repo_root, status_cfg)
    if latest_run is None and artifacts_root_path is not None:
        latest_run = _latest_child_dir(repo_root, target["artifacts_root"], "artifacts_root")
    latest_run_is_artifacts_root = (
        latest_run is not None
        and artifacts_root_path is not None
        and latest_run == artifacts_root_path
    )
    status_path = _status_path(repo_root, status_cfg, "log", latest_run)
    if status_path is not None:
        return status_path
    if isinstance(target.get("logs_root"), str):
        direct_log = _configured_existing_log_file_path(repo_root, target["logs_root"])
        if direct_log is not None:
            return direct_log
        start_log = _start_log_path(repo_root, target)
        if latest_run_is_artifacts_root and start_log is not None and start_log.is_file():
            return start_log
        if latest_run is not None:
            latest_run_log = _latest_log_path(repo_root, str(latest_run))
            if latest_run_log is not None:
                return latest_run_log
        if start_log is not None and start_log.is_file():
            return start_log
        return _latest_log_path(repo_root, target["logs_root"])
    return None


def _status_path(
    repo_root: Path, status_cfg: dict, key: str, latest_run: Optional[Path]
) -> Optional[Path]:
    raw = status_cfg.get(key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    path = Path(os.path.expanduser(raw))
    if path.is_absolute():
        return _project_scoped_path(repo_root, path, f"status.{key}")
    if latest_run is not None:
        return _project_scoped_path(repo_root, latest_run / path, f"status.{key}")
    return _project_scoped_path(repo_root, repo_root / path, f"status.{key}")


def _latest_run_dir(repo_root: Path, status_cfg: dict) -> Optional[Path]:
    raw = status_cfg.get("run_dir_glob")
    if not isinstance(raw, str) or not raw.strip():
        return None
    pattern = Path(os.path.expanduser(raw))
    if not pattern.is_absolute():
        pattern = repo_root / pattern
    _project_scoped_path(repo_root, _glob_static_root(pattern), "status.run_dir_glob")
    pattern_text = str(pattern)
    matches = [Path(item) for item in glob.glob(pattern_text)]
    mtimes: List[tuple[float, Path]] = []
    for path in matches:
        try:
            scoped = _project_scoped_path(repo_root, path, "status.run_dir_glob")
            if scoped.is_dir():
                mtimes.append((scoped.stat().st_mtime, scoped))
        except OSError:
            continue
    if not mtimes:
        return None
    return max(mtimes, key=lambda item: item[0])[1]


def _latest_child_dir(repo_root: Path, raw_root: str, label: str) -> Optional[Path]:
    root = _project_scoped_config_path(repo_root, raw_root, label)
    if not root.is_dir():
        return root if root.exists() else None
    candidates: List[tuple[float, Path]] = []
    try:
        for child in root.iterdir():
            if child.is_dir():
                candidates.append((child.stat().st_mtime, child))
    except OSError:
        return None
    return max(candidates, key=lambda item: item[0])[1] if candidates else root


def _latest_log_path(repo_root: Path, raw_root: str) -> Optional[Path]:
    root = _project_scoped_config_path(repo_root, raw_root, "logs_root")
    if root.is_file():
        return root
    if not root.is_dir():
        return None
    candidates: List[tuple[float, Path]] = []
    log_candidates: List[tuple[float, Path]] = []
    try:
        for child in root.rglob("*"):
            try:
                scoped_child = _project_scoped_path(repo_root, child, "logs_root")
            except DevError:
                continue
            if scoped_child.is_file():
                item = (scoped_child.stat().st_mtime, scoped_child)
                candidates.append(item)
                if scoped_child.suffix == ".log":
                    log_candidates.append(item)
    except OSError:
        return None
    pool = log_candidates or candidates
    return max(pool, key=lambda item: item[0])[1] if pool else None


def _project_scoped_config_path(repo_root: Path, raw: str, label: str) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = repo_root / path
    return _project_scoped_path(repo_root, path, label)


def _glob_static_root(pattern: Path) -> Path:
    prefix: List[str] = []
    for part in pattern.parts:
        if glob.has_magic(part):
            break
        prefix.append(part)
    return Path(*prefix) if prefix else Path(".")


def _run_shell(command: str, cwd: Path) -> int:
    result = subprocess.run(command, shell=True, cwd=str(cwd), check=False)  # noqa: S602
    return int(result.returncode)


def _start_log_path(repo_root: Path, target: dict) -> Optional[Path]:
    raw = target.get("logs_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    direct_log = _configured_log_file_path(repo_root, raw)
    if direct_log is not None:
        return direct_log
    root = _project_scoped_config_path(repo_root, raw, "logs_root")
    name = f"{_state_safe(str(target['kind']))}-{_state_safe(str(target['name']))}.log"
    return _project_scoped_path(repo_root, root / name, "logs_root")


def _configured_log_file_path(repo_root: Path, raw: str) -> Optional[Path]:
    root = _project_scoped_config_path(repo_root, raw, "logs_root")
    if root.exists() and root.is_file():
        return root
    return root if root.suffix == ".log" else None


def _configured_existing_log_file_path(repo_root: Path, raw: str) -> Optional[Path]:
    root = _project_scoped_config_path(repo_root, raw, "logs_root")
    return root if root.is_file() else None


def _start_process(command: str, cwd: Path, log_path: Optional[Path] = None) -> int:
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    stdout_handle = None
    try:
        if log_path is not None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_handle = log_path.open("ab")
                stdout = stdout_handle
                stderr = subprocess.STDOUT
            except OSError:
                stdout_handle = None
        proc = subprocess.Popen(  # noqa: S602
            command,
            shell=True,
            cwd=str(cwd),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
        )
        return int(proc.pid)
    finally:
        if stdout_handle is not None:
            # Popen duped the fd for the child; close the parent's copy before returning.
            stdout_handle.close()


def _state_dir(repo_root: Path) -> Path:
    if shutil.which("git") is None:
        return (repo_root / ".agenttools-dev").resolve()
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-path", "agenttools-dev"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip())
        if not path.is_absolute():
            path = repo_root / path
        return path.resolve()
    return (repo_root / ".agenttools-dev").resolve()


def _state_path(repo_root: Path, kind: str, name: str) -> Path:
    safe_kind = _state_safe(kind)
    safe_name = _state_safe(name)
    return _state_dir(repo_root) / f"{safe_kind}-{safe_name}.json"


def _state_safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "target"


def _write_state(repo_root: Path, record: dict) -> None:
    path = _state_path(repo_root, str(record["kind"]), str(record["name"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")


def _read_states(repo_root: Path) -> List[tuple[dict, Path]]:
    directory = _state_dir(repo_root)
    if not directory.is_dir():
        return []
    records: List[tuple[dict, Path]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            records.append((data, path))
    return records


def _state_for_target(
    repo_root: Path, kind: str, name: str
) -> tuple[Optional[dict], Optional[Path]]:
    path = _state_path(repo_root, kind, name)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    return data, path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


EXIT_BAD_CAPTURE = 3
# Distinct from EXIT_BAD_CAPTURE so a caller can tell "retake the screenshot" from "the
# screenshot is fine, stage your change" - retrying a capture never fixes a binding failure.
EXIT_UNBOUND_PROOF = 4


def _parse_viewport(raw: str) -> tuple[int, int]:
    parts = raw.lower().split("x")
    if len(parts) != 2:
        raise DevError(f"--viewport must be WIDTHxHEIGHT, got {raw!r}")
    try:
        width, height = int(parts[0], 10), int(parts[1], 10)
    except ValueError as exc:
        raise DevError(f"--viewport must be WIDTHxHEIGHT, got {raw!r}") from exc
    if width <= 0 or height <= 0:
        raise DevError("--viewport dimensions must be positive")
    return width, height


def _cmd_shot(args: argparse.Namespace) -> int:
    """Capture, measure, and attest - the whole cycle, so the caller never attests alone.

    Heavy work is imported lazily here to keep module import stdlib-only and `dev --help` fast.
    """
    import hashlib

    from . import visual_proof

    width, height = _parse_viewport(args.viewport)
    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        raise DevError(f"--repo is not a directory: {repo}")
    # Bind to the repository ROOT, matching every other command, so a capture taken from a
    # subdirectory still records the path the gate resolves at commit time.
    try:
        repo = _find_repo_root(repo)
    except DevError:
        pass  # not a git repo: write_attestation refuses below, with a clearer message

    try:
        context = visual_proof.capture_with_sweep(
            args.url, args.out, width, height, sweep_step=args.sweep_step
        )
        # Read ONCE, then measure and hash the same bytes: re-reading would let a concurrent
        # writer swap the file so the record describes bytes the verdict never judged.
        payload = Path(args.out).read_bytes()
        metrics = visual_proof.measure_png_bytes(payload, source=args.out)
    except visual_proof.MissingBackendError as exc:
        raise DevError(str(exc), exit_code=EXIT_MISSING_DEP) from exc
    except visual_proof.CaptureError as exc:
        raise DevError(str(exc), exit_code=EXIT_BAD_CAPTURE) from exc
    except OSError as exc:
        raise DevError(
            f"no capture file to measure at {args.out}: {exc}", exit_code=EXIT_BAD_CAPTURE
        ) from exc

    verdict = visual_proof.verdict_for(
        metrics, expect_width=width, expect_height=context.page_height
    )
    attested: Optional[str] = None
    if verdict.ok and not args.no_attest:
        digest = hashlib.sha256(payload).hexdigest()
        try:
            attested = str(
                visual_proof.write_attestation(
                    args.out, verdict, repo, context.url, capture_sha256=digest
                )
            )
        except visual_proof.UnboundProofError as exc:
            raise DevError(str(exc), exit_code=EXIT_UNBOUND_PROOF) from exc
        except OSError as exc:
            raise DevError(
                f"captured {args.out} but could not write the proof record: {exc}",
                # The capture is good; a full disk or unwritable cache is not fixed by
                # retaking it, so this must not read as "bad capture" to an automated caller.
                exit_code=EXIT_UNBOUND_PROOF,
            ) from exc

    if args.json:
        print(
            json.dumps(
                {
                    "ok": verdict.ok,
                    "capture": str(Path(args.out).resolve()),
                    "width": metrics.width,
                    "height": metrics.height,
                    "uniform_frac": round(metrics.uniform_frac, 4),
                    "largest_band_px": metrics.largest_band,
                    "band_frac": round(metrics.band_frac, 4),
                    "reasons": list(verdict.reasons),
                    "attestation": attested,
                },
                indent=2,
            )
        )
    elif verdict.ok:
        print(
            f"captured {metrics.width}x{metrics.height} -> {args.out} "
            f"(largest blank band {metrics.largest_band}px, {metrics.band_frac:.1%})"
        )
        print(f"proof recorded: {attested}" if attested else "proof not recorded (--no-attest)")
    else:
        print(f"capture rejected - no proof written: {args.out}", file=sys.stderr)
        for reason in verdict.reasons:
            print(f"  - {reason}", file=sys.stderr)

    return 0 if verdict.ok else EXIT_BAD_CAPTURE


def _cmd_env(args: argparse.Namespace) -> int:
    try:
        base = _find_repo_root(Path.cwd())
    except DevError:
        base = Path.cwd()
    project = _normalize_project_path(args.add_project, base)
    existing = [
        item for item in os.environ.get(DEV_PROJECT_PATHS_ENV, "").split(os.pathsep) if item
    ]
    project_text = str(project)
    if project_text not in existing:
        existing.append(project_text)
    print(f"export {DEV_PROJECT_PATHS_ENV}={_shell_single_quote(os.pathsep.join(existing))}")
    return 0


def _load_cleanup_rules_or_raise() -> dict:
    try:
        return cleanup_rules.load_rules()
    except cleanup_rules.RuleError as exc:
        raise DevError(str(exc)) from exc


def _cmd_kill_orphan(args: argparse.Namespace) -> int:
    rules = _load_cleanup_rules_or_raise()
    try:
        target = cleanup_rules.resolve_kill_target(rules, args.harness, args.worker, args.instance)
        # Selection (which processes match, and the max_matches refusal) lives in
        # cleanup_rules.KillTarget.select_matches — NOT reimplemented here — so that decision
        # is covered by the same review-quorum gate as the rest of the matching logic.
        matches = target.select_matches(_list_all_processes())
    except cleanup_rules.RuleError as exc:
        raise DevError(str(exc)) from exc
    if not matches:
        print(f"dev kill-orphan: no processes matched {target.describe()}")
        return 0
    # Audit intent BEFORE acting: if termination is interrupted partway (a pid vanishes, a
    # permission error), the record of what we were about to do still exists.
    _audit_cleanup_or_raise(
        "kill-orphan",
        {
            "family": target.family,
            "worker": target.worker,
            "instance": target.instance,
            "method": target.method,
            "intended": [{"pid": pid, "command": command} for pid, command in matches],
        },
    )
    killed, already_gone, failed = _terminate_kill_target_matches(matches, target)
    print(
        f"dev kill-orphan: {target.describe()} method={target.method} -> "
        f"killed {len(killed)}/{len(matches)} process(es): {', '.join(str(pid) for pid in killed)}"
    )
    if already_gone:
        print(
            f"dev kill-orphan: already gone by signal time (target already achieved): "
            f"{', '.join(str(pid) for pid in already_gone)}"
        )
    if failed:
        print(
            f"dev kill-orphan: FAILED to signal {len(failed)} process(es): "
            f"{', '.join(str(pid) for pid in failed)}"
        )
    _audit_cleanup_or_raise(
        "kill-orphan-result",
        {
            "family": target.family,
            "worker": target.worker,
            "instance": target.instance,
            "killed_pids": killed,
            "already_gone_pids": already_gone,
            "failed_pids": failed,
        },
    )
    if failed:
        raise DevError(
            f"{len(failed)} process(es) could not be signaled; see the FAILED line above"
        )
    return 0


def _terminate_kill_target_matches(
    matches: List[Tuple[int, str]], target: "cleanup_rules.KillTarget"
) -> Tuple[List[int], List[int], List[int]]:
    """Send the configured signal(s) to every matched pid.

    Returns (killed_pids, already_gone_pids, failed_pids) — mirroring _delete_log_files'
    removed/already_absent/failed triad, so both cleanup commands report partial outcomes with
    the same shape. Re-validates each pid's command line immediately before signaling (both
    the initial signal and any SIGKILL escalation) rather than trusting the snapshot taken at
    match time — a pid can be reused by an unrelated process in the window between enumeration
    and signaling, or during the sigterm_then_sigkill grace period. A pid that no longer
    matches (already dead, or reused by something else) is BENIGN — the goal (that target
    gone) is already achieved — and goes in already_gone_pids, not failed_pids. A real signal
    failure (permission denied) goes in failed_pids. Neither aborts signaling the rest of the
    batch.
    """
    initial_sig = signal.SIGKILL if target.method == "sigkill" else signal.SIGTERM
    killed: List[int] = []
    already_gone: List[int] = []
    failed: List[int] = []
    for pid, _snapshot_command in matches:
        if not _revalidate_kill_match(pid, target):
            already_gone.append(pid)
            continue
        if _try_send_signal(pid, initial_sig):
            killed.append(pid)
        else:
            failed.append(pid)
    if target.method == "sigterm_then_sigkill" and target.grace_seconds > 0 and killed:
        time.sleep(target.grace_seconds)
        for pid in killed:
            if not _pid_alive(pid):
                continue
            if not _revalidate_kill_match(pid, target):
                continue
            _try_send_signal(pid, signal.SIGKILL)
    return killed, already_gone, failed


def _try_send_signal(pid: int, sig: signal.Signals) -> bool:
    try:
        _send_signal(pid, sig)
        return True
    except DevError:
        return False


def _revalidate_kill_match(pid: int, target: "cleanup_rules.KillTarget") -> bool:
    """Re-fetch and re-match the pid's CURRENT command line against target's own pattern.

    Deliberately does not compare against the enumeration-time snapshot (no "expected_command"
    parameter): BSD `ps` truncates the command column unless invoked with a wide-output flag,
    so two separately-invoked `ps` calls for the same still-matching process are not
    guaranteed byte-identical — a string-equality check would false-negative on a perfectly
    legitimate, still-live target. Re-matching against the pattern is also correct if the pid
    was reused by a DIFFERENT process that still matches the family (still a legitimate kill
    target).
    """
    try:
        current_command = _process_command(pid)
    except DevError:
        return False
    return target.matches(current_command)


def _cmd_clean_logs(args: argparse.Namespace) -> int:
    rules = _load_cleanup_rules_or_raise()
    try:
        targets = cleanup_rules.resolve_clean_log_paths(rules, args.paths)
    except cleanup_rules.RuleError as exc:
        raise DevError(str(exc)) from exc
    _audit_cleanup_or_raise("clean-logs", {"intended_paths": [str(p) for p in targets]})
    removed, already_absent, failed = _delete_log_files(targets)
    # Print the summary BEFORE the result-audit write: if the audit write itself fails, the
    # user still learns what was actually deleted rather than seeing only a raw DevError.
    _print_clean_logs_summary(removed, already_absent, failed)
    _audit_cleanup_or_raise(
        "clean-logs-result",
        {"removed": removed, "already_absent": already_absent, "failed": failed},
    )
    if failed:
        # Detail already printed by _print_clean_logs_summary above — keep this message short
        # so the failure isn't reported twice.
        raise DevError(f"{len(failed)} file(s) could not be deleted; see the FAILED line above")
    return 0


def _delete_log_files(targets: List[Path]) -> Tuple[List[str], List[str], List[str]]:
    removed: List[str] = []
    already_absent: List[str] = []
    failed: List[str] = []
    for path in targets:
        try:
            path.unlink()
            removed.append(str(path))
        except FileNotFoundError:
            already_absent.append(str(path))
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    return removed, already_absent, failed


def _print_clean_logs_summary(
    removed: List[str], already_absent: List[str], failed: List[str]
) -> None:
    if removed:
        print(f"dev clean-logs: removed {len(removed)} file(s): {', '.join(removed)}")
    if already_absent:
        print(
            f"dev clean-logs: already absent {len(already_absent)} file(s): "
            f"{', '.join(already_absent)}"
        )
    if failed:
        print(f"dev clean-logs: FAILED to remove {len(failed)} file(s): {', '.join(failed)}")
    if not removed and not already_absent and not failed:
        print("dev clean-logs: nothing to do")


def _audit_cleanup_or_raise(action: str, detail: dict) -> None:
    """Append one JSON line to the machine-wide cleanup audit log.

    Deliberately NOT scoped to a repo (unlike _state_dir, which is per-repo/worktree state):
    a kill-orphan/clean-logs call can target a process or file with no relationship to the
    caller's cwd, so tying the audit trail to `_find_repo_root(Path.cwd())` would make it
    fail (or write to the wrong place) for the exact cases these commands exist to handle
    (e.g. `cd /tmp && dev clean-logs /tmp/x.log`).

    Raises DevError (not a raw OSError) on failure, so main()'s single except DevError clause
    still produces the CLI's normal error contract instead of a traceback — this call happens
    BEFORE any kill/delete, so an unwritable state dir fails the whole command closed rather
    than acting with no audit trail.
    """
    directory = _global_state_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), "action": action, **detail}
        with (directory / "cleanup-audit.log").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError as exc:
        raise DevError(f"could not write cleanup audit log to {directory}: {exc}") from exc


def _global_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        # XDG requires an absolute path; a relative/unset value falls back to the spec default
        # rather than silently resolving against whatever the current cwd happens to be.
        base = str(Path.home() / ".local" / "state")
    return Path(base) / "dev-cli"


def _cmd_stop(args: argparse.Namespace) -> int:
    repo_root = _find_repo_root(Path.cwd())
    if args.target and (args.pid is not None or args.port is not None or args.pgid is not None):
        raise DevError(
            "pass either a dev target name, --pid, --port, or --pgid; not multiple targets"
        )
    if args.pgid is not None:
        return _stop_process_group(repo_root, args.pgid)
    if args.target:
        config = _load_rig_config(repo_root)
        target = _configured_target(config, args.target)
        return _stop_target(repo_root, config, target)
    elif args.pid is not None:
        pid = args.pid
    elif args.port is not None:
        pid = _pid_for_port(args.port)
    else:
        raise DevError("pass a dev target name, --pid, --port, or --pgid")
    allowed_roots = _allowed_project_roots(repo_root)
    _terminate_pid(pid, allowed_roots)
    print(f"sent SIGTERM to pid {pid}")
    return 0


def _stop_target(repo_root: Path, config: dict, target: dict) -> int:
    record, record_path = _state_for_target(repo_root, target["kind"], target["name"])
    if record is not None:
        pid = int(record.get("pid", 0) or 0)
        if pid <= 0:
            raise DevError(f"state for {target['name']} does not contain a valid pid")
        if not _pid_alive(pid):
            if record_path is not None:
                try:
                    record_path.unlink()
                except FileNotFoundError:
                    pass
            raise DevError(f"dev target {target['name']} is not running; stale state removed")
        pgid = int(record.get("pgid", 0) or 0)
        pids = [pid]
    elif target.get("ports"):
        pids = _pids_for_ports([int(port) for port in target["ports"]])
        pgid = 0
    else:
        raise DevError(f"no running state for {target['name']} and no configured port to resolve")
    allowed_roots = _allowed_project_roots(repo_root)
    if pgid > 0:
        _stop_process_group(repo_root, pgid)
    else:
        for pid in pids:
            _terminate_pid(pid, allowed_roots)
    if record_path is not None:
        try:
            record_path.unlink()
        except FileNotFoundError:
            pass
    if pgid <= 0:
        print(
            f"sent SIGTERM to pid{'s' if len(pids) != 1 else ''} {', '.join(str(pid) for pid in pids)}"
        )
    return 0


def _pids_for_ports(ports: Sequence[int]) -> List[int]:
    pids: List[int] = []
    errors: List[str] = []
    for port in ports:
        try:
            pid = _pid_for_port(port)
        except DevError as exc:
            errors.append(str(exc))
            continue
        if pid not in pids:
            pids.append(pid)
    if not pids:
        detail = "; ".join(errors) if errors else "no ports configured"
        raise DevError(f"no running process found for configured ports: {detail}")
    return pids


def _stop_process_group(repo_root: Path, pgid: int) -> int:
    pids = _process_group_members(pgid)
    if not pids:
        raise DevError(f"no processes found in process group {pgid}")
    allowed_roots = _allowed_project_roots(repo_root)
    for pid in pids:
        _inspect_validated_process_if_present(pid, allowed_roots)
    latest_pids = _process_group_members(pgid)
    if set(latest_pids) != set(pids):
        for pid in latest_pids:
            _inspect_validated_process_if_present(pid, allowed_roots)
    # Best effort: a process can still join the group after this final inspection.
    _send_process_group_signal(pgid, signal.SIGTERM)
    print(f"sent SIGTERM to process group {pgid}")
    return 0


def _terminate_pid(pid: int, allowed_roots: Sequence[Path]) -> None:
    _inspect_validated_process(pid, allowed_roots)
    _send_signal(pid, signal.SIGTERM)


def _inspect_validated_process(pid: int, allowed_roots: Sequence[Path]) -> None:
    _validate_stop_target(
        pid=pid,
        cwd=_process_cwd(pid),
        command=_process_command(pid),
        allowed_roots=allowed_roots,
    )


def _inspect_validated_process_if_present(pid: int, allowed_roots: Sequence[Path]) -> bool:
    try:
        cwd = _process_cwd(pid)
        command = _process_command(pid)
    except DevError:
        if not _pid_alive(pid):
            return False
        raise
    _validate_stop_target(pid=pid, cwd=cwd, command=command, allowed_roots=allowed_roots)
    return True


def _extra_project_roots(repo_root: Path) -> List[Path]:
    roots: List[Path] = []
    raw = os.environ.get(DEV_PROJECT_PATHS_ENV, "")
    for item in raw.split(os.pathsep):
        if item.strip():
            roots.append(_normalize_project_path(item, repo_root))
    return roots


def _allowed_project_roots(repo_root: Path) -> List[Path]:
    return [repo_root.resolve(), *_extra_project_roots(repo_root)]


def _normalize_project_path(raw: str, base: Path) -> Path:
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _validate_stop_target(
    *, pid: int, cwd: Optional[Path], command: str, allowed_roots: Sequence[Path]
) -> None:
    if not _is_dev_tool(command):
        raise DevError(
            f"pid {pid} is not recognized as a development tool; command was: {command or '<unknown>'}"
        )
    if not _is_project_scoped(cwd, command, allowed_roots):
        roots = ", ".join(str(root) for root in allowed_roots)
        raise DevError(
            f"pid {pid} does not appear scoped to the current project. "
            f"cwd={cwd or '<unknown>'}; command={command or '<unknown>'}; allowed roots: {roots}"
        )


_DEV_TOOL_NAMES = frozenset(
    {
        "air",
        "astro",
        "bun",
        "cargo",
        "cypress",
        "django-admin",
        "docker",
        "docker-compose",
        "detox",
        "expo",
        "fastapi",
        "flask",
        "foreman",
        "go",
        "make",
        "next",
        "node",
        "nodemon",
        "npm",
        "nuxt",
        "playwright",
        "pnpm",
        "pytest",
        "quasar",
        "rails",
        "rake",
        "react-native",
        "reflex",
        "storybook",
        "streamlit",
        "ts-node",
        "tsx",
        "turbo",
        "tox",
        "uv",
        "vite",
        "vite-node",
        "vitest",
        "webpack",
        "webpack-dev-server",
        "yarn",
    }
)
_WRAPPERS = frozenset({"env", "time", "timeout", "gtimeout", "nice", "nohup", "stdbuf"})
_WRAPPER_OPT_ARGS = {
    "env": frozenset({"-S", "-u", "--unset"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
    "gtimeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
}
_DESTRUCTIVE_HEADS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "fdisk",
        "format",
        "kill",
        "killall",
        "mkfs",
        "mount",
        "mv",
        "pkill",
        "rm",
        "rmdir",
        "shred",
        "sudo",
        "umount",
        "wipefs",
    }
)
_PYTHON_RE = re.compile(r"python(?:\d+(?:\.\d+)?)?$")
_SHELL_HEADS = frozenset({"sh", "bash", "zsh"})


def _ensure_lifecycle_command(command: str, repo_root: Optional[Path] = None) -> None:
    segments = _command_segments(command)
    if (
        not segments
        or _has_shell_substitution(command)
        or _has_shell_redirection(command)
        or any(
            _has_destructive_head(segment, repo_root) or not _is_dev_tool(segment, repo_root)
            for segment in segments
        )
    ):
        raise DevError(
            f"{command!r} is not a permitted development/e2e command; use dev.server/dev.e2e "
            "for dev runners only, keep destructive shell commands outside dev, and put "
            "inline redirection, command substitution, or non-dev pipelines in a reviewed "
            "project-local wrapper script."
        )


def _has_destructive_head(command: str, repo_root: Optional[Path] = None) -> bool:
    for segment in _command_segments(command):
        if _segment_has_destructive_head(segment, repo_root):
            return True
    return False


def _segment_has_destructive_head(segment: str, repo_root: Optional[Path] = None) -> bool:
    shell_payload = _shell_command_payload(_split_command(segment))
    if shell_payload is not None:
        return _has_destructive_head(shell_payload, repo_root) or not _segments_are_lifecycle_safe(
            shell_payload, repo_root
        )
    if _has_inline_code_execution(segment):
        return True
    head = _command_head(segment)
    if head in _DESTRUCTIVE_HEADS:
        return True
    if _git_destructive(segment):
        return True
    if _docker_destructive(segment):
        return True
    return _runner_payload_is_destructive(segment, repo_root)


_RUNNER_PAYLOAD_SUBCOMMANDS = frozenset({"exec", "x", "dlx"})
_UV_RUN_SHORT_FLAGS_WITH_VALUE = frozenset({"C", "P", "b", "c", "f", "i", "p", "w"})
# Keep these uv option tables in sync with orchestrator-stays-thin's _UV_TEST_* copies.
# Derived from `uv help run` on 2026-07-09. Unknown flags also emit speculative
# value-skip payload starts, so future value-taking uv flags cannot hide inline or
# destructive payloads behind an unrecognized option operand.
_UV_RUN_BOOLEAN_FLAGS = frozenset(
    {
        "--active",
        "--all-extras",
        "--all-groups",
        "--all-packages",
        "--compile-bytecode",
        "--exact",
        "--frozen",
        "--gui-script",
        "--help",
        "--isolated",
        "--locked",
        "--managed-python",
        "--module",
        "--native-tls",
        "--no-build",
        "--no-build-isolation",
        "--no-binary",
        "--no-cache",
        "--no-config",
        "--no-default-groups",
        "--no-dev",
        "--no-editable",
        "--no-env-file",
        "--no-index",
        "--no-managed-python",
        "--no-progress",
        "--no-project",
        "--no-python-downloads",
        "--no-sources",
        "--no-sync",
        "--offline",
        "--only-dev",
        "--refresh",
        "--reinstall",
        "--script",
        "--upgrade",
        "-U",
        "-h",
        "-m",
        "-n",
        "-q",
        "-s",
        "-v",
    }
)
_UV_RUN_FLAGS_WITH_VALUE = frozenset(
    {
        "--allow-insecure-host",
        "--build-constraint",
        "--cache-dir",
        "--color",
        "--config-file",
        "--config-setting",
        "--config-settings-package",
        "--constraint",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--exclude-newer-package",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--fork-strategy",
        "--group",
        "--index",
        "--index-strategy",
        "--index-url",
        "--keyring-provider",
        "--link-mode",
        "--no-binary-package",
        "--no-build-isolation-package",
        "--no-build-package",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--package",
        "--prerelease",
        "--project",
        "--python",
        "--python-preference",
        "--python-platform",
        "--refresh-package",
        "--reinstall-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
    }
)


def _runner_payload_is_destructive(segment: str, repo_root: Optional[Path] = None) -> bool:
    tokens = _split_command(segment)
    if not tokens:
        return False
    for start in _runner_payload_starts(tokens):
        payload = tokens[start:]
        shell_payload = _shell_command_payload(payload)
        if shell_payload is not None:
            return _has_destructive_head(
                shell_payload, repo_root
            ) or not _segments_are_lifecycle_safe(shell_payload, repo_root)
        for index, token in enumerate(payload):
            head = Path(token).name
            if head in _DESTRUCTIVE_HEADS:
                return True
            if head == "git" and any(
                item in ("reset", "clean", "checkout", "commit", "push")
                for item in payload[index + 1 :]
            ):
                return True
            if (
                head in ("sh", "bash", "zsh")
                and _shell_command_payload(payload[index:]) is not None
            ):
                return True
    return False


def _segments_are_lifecycle_safe(command: str, repo_root: Optional[Path]) -> bool:
    segments = _command_segments(command)
    return (
        bool(segments)
        and not _has_shell_substitution(command)
        and not _has_shell_redirection(command)
    ) and all(
        not _segment_has_destructive_head(segment, repo_root) and _is_dev_tool(segment, repo_root)
        for segment in segments
    )


def _shell_command_payload(tokens: Sequence[str]) -> Optional[str]:
    if not tokens or Path(tokens[0]).name not in _SHELL_HEADS:
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if not (token.startswith("-") or token.startswith("+")):
            return None
        if token == "--":
            return None
        if _shell_flag_includes_c(token):
            if index + 1 < len(tokens):
                return tokens[index + 1]
            return ""
        index += 2 if _shell_flag_takes_value(token) else 1
    return None


def _shell_flag_includes_c(token: str) -> bool:
    if token == "-c":
        return True
    if token.startswith("--"):
        return False
    return token.startswith("-") and "c" in token[1:]


def _shell_flag_takes_value(token: str) -> bool:
    if token in ("-o", "+o", "-O", "+O"):
        return True
    if token.startswith("--"):
        return token in ("--rcfile", "--init-file")
    return bool(set(token[1:]) & {"o", "O"})


def _runner_payload_starts(tokens: Sequence[str]) -> Iterable[int]:
    for index, token in enumerate(tokens[:-1]):
        head = Path(token).name
        subcommand = tokens[index + 1]
        if head in ("npm", "pnpm", "yarn", "bun") and subcommand in _RUNNER_PAYLOAD_SUBCOMMANDS:
            yield index + 2
        if head == "uv" and subcommand == "run":
            yield from _uv_run_payload_starts(tokens, index + 2)


def _uv_run_payload_starts(tokens: Sequence[str], index: int) -> Iterable[int]:
    pending = [index]
    visited: set[int] = set()
    yielded: set[int] = set()
    while pending:
        cursor = min(pending.pop(), len(tokens))
        if cursor in visited:
            continue
        visited.add(cursor)
        while cursor < len(tokens):
            token = tokens[cursor]
            if token == "--":
                cursor += 1
                break
            if token == "-" or not token.startswith("-"):
                break
            if token.startswith("--"):
                flag = token.split("=", 1)[0]
                if "=" in token:
                    cursor += 1
                elif flag in _UV_RUN_FLAGS_WITH_VALUE:
                    cursor += 2
                elif flag in _UV_RUN_BOOLEAN_FLAGS:
                    cursor += 1
                else:
                    pending.append(cursor + 2)
                    cursor += 1
            elif _uv_short_flag_takes_following_value(token):
                cursor += 2
            elif _uv_short_flag_has_attached_value(token):
                cursor += 1
            elif _uv_short_flag_is_known_boolean(token):
                cursor += 1
            else:
                pending.append(cursor + 2)
                cursor += 1
        cursor = min(cursor, len(tokens))
        if cursor not in yielded:
            yielded.add(cursor)
            yield cursor


def _uv_short_flag_takes_following_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    for pos, flag in enumerate(flags):
        if flag in _UV_RUN_SHORT_FLAGS_WITH_VALUE:
            return pos == len(flags) - 1
    return False


def _uv_short_flag_has_attached_value(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    flags = token[1:]
    return any(
        flag in _UV_RUN_SHORT_FLAGS_WITH_VALUE and pos < len(flags) - 1
        for pos, flag in enumerate(flags)
    )


def _uv_short_flag_is_known_boolean(token: str) -> bool:
    if token.startswith("--") or not token.startswith("-") or token == "-":
        return False
    return all(f"-{flag}" in _UV_RUN_BOOLEAN_FLAGS for flag in token[1:])


def _command_segments(command: str) -> List[str]:
    segments: List[str] = []
    buf: List[str] = []
    quote: Optional[str] = None
    i = 0
    while i < len(command):
        char = command[i]
        prev = command[i - 1] if i > 0 else ""
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if quote is not None:
            buf.append(char)
            if char == quote:
                quote = None
            i += 1
        elif char in ("'", '"'):
            quote = char
            buf.append(char)
            i += 1
        elif command[i : i + 2] in ("&&", "||"):
            segments.append("".join(buf).strip())
            buf = []
            i += 2
        elif char == "&" and prev not in ("&", ">") and nxt not in ("&", ">"):
            segments.append("".join(buf).strip())
            buf = []
            i += 1
        elif char in (";", "|", "\n"):
            segments.append("".join(buf).strip())
            buf = []
            i += 1
        else:
            buf.append(char)
            i += 1
    segments.append("".join(buf).strip())
    return [segment for segment in segments if segment]


_SHELL_SUBSTITUTION_RE = re.compile(r"\$\(|`|[<>]\(")


def _has_shell_substitution(command: str) -> bool:
    return bool(_SHELL_SUBSTITUTION_RE.search(_blank_single_quoted(command)))


def _has_shell_redirection(command: str) -> bool:
    quote: Optional[str] = None
    escaped = False
    for char in command:
        if quote is not None:
            if quote == '"' and escaped:
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = None
            continue
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in ("'", '"'):
            quote = char
            continue
        if char in ("<", ">"):
            return True
    return False


def _blank_single_quoted(command: str) -> str:
    out: List[str] = []
    quote = False
    for char in command:
        if quote:
            out.append("'" if char == "'" else " ")
            if char == "'":
                quote = False
        elif char == "'":
            quote = True
            out.append(char)
        else:
            out.append(char)
    return "".join(out)


def _git_destructive(segment: str) -> bool:
    toks = _split_command(segment)
    if not toks or Path(toks[0]).name != "git":
        return False
    return any(tok in ("reset", "clean", "checkout", "commit", "push") for tok in toks[1:])


def _docker_destructive(segment: str) -> bool:
    toks = _split_command(segment)
    for index, token in enumerate(toks[:-1]):
        head = Path(token).name
        rest = toks[index + 1 :]
        if head == "docker-compose":
            return bool(rest) and rest[0] in {"down", "rm", "kill", "stop"}
        if head != "docker":
            continue
        if rest[:2] in (
            ["system", "prune"],
            ["compose", "down"],
            ["compose", "rm"],
            ["compose", "stop"],
        ):
            return True
        if len(rest) >= 2 and rest[0] in {"volume", "image", "container", "network"}:
            return rest[1] in {"rm", "prune"}
        if rest and rest[0] in {"rm", "rmi", "kill", "stop"}:
            return True
    return False


def _is_dev_tool(command: str, repo_root: Optional[Path] = None) -> bool:
    tokens = _split_command(command)
    shell_payload = _shell_command_payload(tokens)
    if shell_payload is not None:
        return _segments_are_lifecycle_safe(shell_payload, repo_root)
    head = _command_head(command)
    if head is None:
        return False
    if head in _DEV_TOOL_NAMES or bool(_PYTHON_RE.fullmatch(head)):
        return True
    return repo_root is not None and _has_project_scoped_command_path(command, repo_root)


def _has_project_scoped_command_path(command: str, repo_root: Path) -> bool:
    for path in _command_paths(command, repo_root):
        if _path_inside(path, repo_root):
            return True
    return False


def _command_head(command: str) -> Optional[str]:
    tokens = _split_command(command)
    if not tokens:
        return None
    i = 0
    while i < len(tokens) and _is_assignment(tokens[i]):
        i += 1
    while i < len(tokens):
        head = Path(tokens[i]).name
        if head in _WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                opt = tokens[i]
                i += 1
                if opt in _WRAPPER_OPT_ARGS.get(head, ()) and i < len(tokens):
                    i += 1
            if head in ("timeout", "gtimeout") and i < len(tokens):
                i += 1
            while i < len(tokens) and _is_assignment(tokens[i]):
                i += 1
            continue
        return head
    return None


def _is_assignment(token: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token))


def _has_inline_code_execution(segment: str) -> bool:
    tokens = _split_command(segment)
    starts: List[int] = []
    direct = _command_start_index(tokens)
    if direct is not None:
        starts.append(direct)
    starts.extend(_runner_payload_starts(tokens))
    return any(_tokens_start_inline_code(tokens[start:]) for start in starts)


def _tokens_start_inline_code(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    head = Path(tokens[0]).name
    rest = tokens[1:]
    if _PYTHON_RE.fullmatch(head):
        return any(token in {"-c", "-"} for token in rest)
    if head == "node":
        return any(
            token in {"-e", "--eval", "-p", "--print", "-"} or token.startswith("-e")
            for token in rest
        )
    return False


def _command_start_index(tokens: Sequence[str]) -> Optional[int]:
    i = 0
    while i < len(tokens) and _is_assignment(tokens[i]):
        i += 1
    while i < len(tokens):
        head = Path(tokens[i]).name
        if head in _WRAPPERS:
            i += 1
            while i < len(tokens) and tokens[i].startswith("-"):
                opt = tokens[i]
                i += 1
                if opt in _WRAPPER_OPT_ARGS.get(head, ()) and i < len(tokens):
                    i += 1
            if head in ("timeout", "gtimeout") and i < len(tokens):
                i += 1
            while i < len(tokens) and _is_assignment(tokens[i]):
                i += 1
            continue
        return i
    return None


def _is_project_scoped(cwd: Optional[Path], command: str, allowed_roots: Sequence[Path]) -> bool:
    if cwd is not None:
        return any(_path_inside(cwd, root) for root in allowed_roots)
    return False


def _command_paths(command: str, cwd: Optional[Path]) -> Iterable[Path]:
    for token in _split_command(command):
        candidates = [token]
        if "=" in token:
            candidates.append(token.split("=", 1)[1])
        for raw in candidates:
            raw = raw.strip()
            if not raw or raw.startswith("-"):
                continue
            if raw.startswith("file://"):
                raw = raw[7:]
            elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw):
                continue
            if raw.startswith("/") or raw.startswith(".") or "/" in raw:
                path = Path(os.path.expanduser(raw))
                if not path.is_absolute() and cwd is not None:
                    path = cwd / path
                yield path


def _split_command(command: str) -> List[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _project_scoped_path(repo_root: Path, path: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    if any(_path_inside(resolved, root) for root in _allowed_project_roots(repo_root)):
        return resolved
    raise DevError(f"{label} points outside the current project or DEV_PROJECT_PATHS: {path}")


def _pid_for_port(
    port: int,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    if shutil.which("lsof") is None:
        raise DevError("cannot resolve --port because lsof is not installed", EXIT_MISSING_DEP)
    result = runner(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise DevError(f"no listening process found on TCP port {port}")
    pids: List[int] = []
    bad: Optional[str] = None
    for line in result.stdout.splitlines():
        item = line.strip()
        if not item:
            continue
        try:
            pid = int(item, 10)
        except ValueError:
            bad = item
            break
        if pid not in pids:
            pids.append(pid)
    if bad is not None:
        raise DevError(f"lsof returned an invalid pid for port {port}: {bad!r}")
    if len(pids) > 1:
        raise DevError(
            f"multiple listening processes found on TCP port {port}: "
            f"{', '.join(str(pid) for pid in pids)}"
        )
    if not pids:
        raise DevError(f"no listening process found on TCP port {port}")
    return pids[0]


def _process_cwd(pid: int) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            return Path(line[1:]).resolve()
    return None


def _process_command(pid: int) -> str:
    # -ww = unlimited output width. Without it BSD ps truncates the command column to
    # terminal width, and a long argv (VS Code/Electron launch flags routinely exceed 200
    # chars) would compare unequal to a `ps -axww` snapshot taken elsewhere (e.g.
    # _list_all_processes) even for the exact same still-running process.
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DevError(
            "cannot inspect process command because ps is not installed", EXIT_MISSING_DEP
        ) from exc
    if result.returncode != 0:
        raise DevError(f"cannot inspect process command for pid {pid}")
    return result.stdout.strip()


def _list_all_processes() -> List[Tuple[int, str]]:
    """Enumerate (pid, full command line) for every visible process.

    Used only by ``dev kill-orphan``. Takes no caller-controlled argument (unlike ``pgrep -f``,
    which interprets the pattern itself and matches an unanchored substring) — matching against
    the ``rules.yaml`` family's anchor regex happens entirely in Python afterward.
    """
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DevError(
            "cannot enumerate processes because ps is not installed", EXIT_MISSING_DEP
        ) from exc
    if result.returncode != 0:
        raise DevError("cannot enumerate processes")
    processes: List[Tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0], 10)
        except ValueError:
            continue
        processes.append((pid, parts[1]))
    return processes


def _process_group_members(pgid: int) -> List[int]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid="],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DevError(
            "cannot inspect process groups because ps is not installed", EXIT_MISSING_DEP
        ) from exc
    if result.returncode != 0:
        raise DevError(f"cannot inspect process group {pgid}")
    members: List[int] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid_value = int(parts[0], 10)
            pgid_value = int(parts[1], 10)
        except ValueError:
            continue
        if pgid_value == pgid:
            members.append(pid_value)
    return members


def _send_signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError as exc:
        raise DevError(f"process {pid} no longer exists") from exc
    except PermissionError as exc:
        raise DevError(f"permission denied sending SIGTERM to pid {pid}") from exc


def _send_process_group_signal(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError as exc:
        raise DevError(f"process group {pgid} no longer exists") from exc
    except PermissionError as exc:
        raise DevError(f"permission denied sending SIGTERM to process group {pgid}") from exc


if __name__ == "__main__":
    sys.exit(main())
