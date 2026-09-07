"""Tests for dev_cli.cleanup_rules and the `dev kill-orphan` / `dev clean-logs` commands.

Run from the repo root::

    python -m pytest tests/test_cleanup_rules.py -q

conftest.py puts the repo root + vendor/ on sys.path.
"""

from __future__ import annotations

import copy
import json
import signal
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from dev_cli import cleanup_rules, cli  # noqa: E402  (sys.path must be set up first)

_SAMPLE_RULES = {
    "version": 1,
    "kill_families": [
        {
            "name": "vscode-e2e-harness",
            "enumerate_argv_contains": "--extensionDevelopmentPath",
            "anchor_flag": "--user-data-dir=",
            "id_template": "hvsc-{worker}-{instance}",
            "worker": {"required": True, "pattern": r"^[0-9]{1,2}$"},
            "instance": {"required": False, "pattern": r"^[0-9a-f]{8}$"},
            "method": "sigkill",
            "grace_seconds": 0,
            "max_matches": 12,
        },
        {
            "name": "graceful-server",
            "enumerate_argv_contains": "fake-server",
            "anchor_flag": "--tag=",
            "id_template": "srv-{worker}-{instance}",
            "worker": {"required": True, "pattern": r"^[0-9]+$"},
            "instance": {"required": True, "pattern": r"^[0-9a-f]{8}$"},
            "method": "sigterm_then_sigkill",
            "grace_seconds": 5,
            "max_matches": 1,
        },
    ],
    "clean_log_rules": [
        {
            "name": "tmp-debug-log",
            "allowed_roots": ["/tmp"],
            "allowed_suffixes": [".log"],
        }
    ],
}


def _write_rules(tmp_path: Path, data: dict = _SAMPLE_RULES) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def wired_cli(tmp_path, monkeypatch):
    """Common CLI test fixture: a rules.yaml on disk, an isolated audit-log dir, and a repo
    root — everything test_cmd_* needs before it adds its own process/signal mocks."""
    rules_path = _write_rules(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    monkeypatch.setattr(cleanup_rules, "DEFAULT_RULES_PATH", rules_path)
    monkeypatch.setattr(cli, "_find_repo_root", lambda _start: repo)
    monkeypatch.setattr(cli, "_global_state_dir", lambda: state_dir)
    return {"tmp_path": tmp_path, "repo": repo, "state_dir": state_dir, "rules_path": rules_path}


def _read_audit_records(state_dir: Path) -> list:
    path = state_dir / "cleanup-audit.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── cleanup_rules.load_rules ─────────────────────────────────────────────────────────────────


def test_load_rules_missing_file_raises(tmp_path):
    with pytest.raises(cleanup_rules.RuleError, match="not found"):
        cleanup_rules.load_rules(tmp_path / "does-not-exist.yaml")


def test_load_rules_invalid_yaml_raises(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("kill_families: [unclosed", encoding="utf-8")
    with pytest.raises(cleanup_rules.RuleError, match="not valid YAML"):
        cleanup_rules.load_rules(path)


def test_load_rules_non_mapping_raises(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(cleanup_rules.RuleError, match="top-level mapping"):
        cleanup_rules.load_rules(path)


def test_load_rules_default_path_reads_the_shipped_rules_yaml():
    """Contract test against the REAL rules.yaml, not a hand-written stand-in — a YAML typo or
    schema violation in the shipped file must be caught here, not first in production."""
    rules = cleanup_rules.load_rules()
    for family in rules["kill_families"]:
        cleanup_rules._validate_kill_family(family)
    for rule in rules["clean_log_rules"]:
        cleanup_rules._validate_clean_log_rule(rule)


# ── cleanup_rules.resolve_kill_target ────────────────────────────────────────────────────────


def test_resolve_kill_target_unknown_family_raises():
    with pytest.raises(cleanup_rules.RuleError, match="unknown --harness"):
        cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "nope", "0", None)


def test_resolve_kill_target_requires_worker_when_marked_required():
    with pytest.raises(cleanup_rules.RuleError, match="requires --worker"):
        cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", None, None)


def test_resolve_kill_target_rejects_worker_not_matching_pattern():
    with pytest.raises(cleanup_rules.RuleError, match="does not match the required pattern"):
        cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "abc", None)


def test_resolve_kill_target_rejects_instance_not_matching_pattern():
    with pytest.raises(cleanup_rules.RuleError, match="does not match the required pattern"):
        cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "0", "short")


def test_resolve_kill_target_requires_instance_when_family_marks_it_required():
    with pytest.raises(cleanup_rules.RuleError, match="requires --instance"):
        cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "graceful-server", "0", None)


def test_resolve_kill_target_full_instance_matches_only_that_instance():
    target = cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "2", "19fd16aa")
    argv = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"
    assert target.matcher.search(argv)
    # A different instance in the same worker slot must NOT match.
    other = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-deadbeef"
    assert not target.matcher.search(other)


def test_resolve_kill_target_worker_only_matches_whole_slot():
    target = cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "0", None)
    a = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-0-aaaaaaaa"
    b = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-0-bbbbbbbb"
    assert target.matcher.search(a)
    assert target.matcher.search(b)


def test_resolve_kill_target_never_matches_bare_substring_without_anchor():
    """The exact gap the two broad screenshots exposed: a decoy carrying the bare `hvsc-0-`
    substring somewhere in argv, but not behind --user-data-dir=, must not match."""
    target = cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "0", None)
    decoy = "code --extensionDevelopmentPath=/tmp/hvsc-cache/watcher"
    assert not target.matcher.search(decoy)


def test_resolve_kill_target_does_not_cross_worker_boundary():
    target = cleanup_rules.resolve_kill_target(_SAMPLE_RULES, "vscode-e2e-harness", "0", None)
    other_worker = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-10-aaaaaaaa"
    assert not target.matcher.search(other_worker)


def test_resolve_kill_target_unknown_method_raises():
    rules = copy.deepcopy(_SAMPLE_RULES)
    rules["kill_families"][0]["method"] = "sigqueue"
    with pytest.raises(cleanup_rules.RuleError, match="unknown method"):
        cleanup_rules.resolve_kill_target(rules, "vscode-e2e-harness", "0", None)


def test_resolve_kill_target_no_kill_families_raises():
    with pytest.raises(cleanup_rules.RuleError, match="no kill_families"):
        cleanup_rules.resolve_kill_target({}, "x", "0", None)


# ── _validate_kill_family: schema/safety-net tests ──────────────────────────────────────────


def test_validate_kill_family_rejects_missing_anchor_flag():
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "id_template": "worker-{worker}",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="anchor_flag"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_empty_enumerate_argv_contains():
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "",
        "anchor_flag": "--x=",
        "id_template": "worker-{worker}",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="enumerate_argv_contains"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_missing_id_template():
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="id_template"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_unknown_template_placeholder():
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "x-{worker}-{slot}",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="unsupported placeholder"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_worker_not_required_when_template_uses_it():
    """A family whose id_template embeds {worker} but doesn't mark that field required would
    let an omitted --worker interpolate the literal 'None' into the match pattern."""
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "worker-{worker}",
        "worker": {"required": False},
    }
    with pytest.raises(cleanup_rules.RuleError, match="required: true"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_instance_field_mismatch_with_template():
    """id_template uses {instance} but there is no 'instance:' field declared — a caller's
    --instance would be silently ignored (dropped by str.format's extra-kwarg tolerance)."""
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "worker-{worker}-{instance}",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="instance"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_sigterm_then_sigkill_with_zero_grace():
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "worker-{worker}-",
        "worker": {"required": True},
        "method": "sigterm_then_sigkill",
        "grace_seconds": 0,
    }
    with pytest.raises(cleanup_rules.RuleError, match="grace_seconds > 0"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_accepts_well_formed_family():
    family = {
        "name": "ok",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "worker-{worker}-{instance}",
        "worker": {"required": True},
        "instance": {"required": False},
        "method": "sigkill",
    }
    cleanup_rules._validate_kill_family(family)  # must not raise


def test_resolve_kill_target_runs_validation_and_raises_ruleerror_not_traceback():
    """A malformed family in rules.yaml must fail with RuleError at the point of use, not a
    raw KeyError/AttributeError several frames down in _build_id_matcher."""
    rules = {
        "kill_families": [
            {
                "name": "broken",
                "enumerate_argv_contains": "node",
                # anchor_flag missing on purpose
                "id_template": "worker-{worker}",
                "worker": {"required": True},
            }
        ]
    }
    with pytest.raises(cleanup_rules.RuleError, match="anchor_flag"):
        cleanup_rules.resolve_kill_target(rules, "broken", "0", None)


@pytest.mark.parametrize(
    "bad_template",
    [
        "x-{worker}-{}",  # empty (positional) placeholder — invisible to \{(\w+)\}
        "x-{worker}-{:.2f}",  # format-spec-only placeholder — also invisible to \{(\w+)\}
    ],
)
def test_validate_kill_family_rejects_non_named_format_placeholders(bad_template):
    """A template using str.format syntax the \\{(\\w+)\\} placeholder scanner can't see (an
    empty {} or a bare format-spec) must fail validation with RuleError via the trial-format
    check, not pass silently and later raise IndexError/ValueError from resolve_kill_target's
    real template.format() call at kill time."""
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": bad_template,
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError, match="not a valid template"):
        cleanup_rules._validate_kill_family(family)


def test_validate_kill_family_rejects_positional_placeholder():
    """{0} IS visible to \\{(\\w+)\\} (digits match \\w), so it's caught by the earlier
    unsupported-placeholder check rather than the trial-format check — still a RuleError
    either way, which is what matters."""
    family = {
        "name": "unsafe",
        "enumerate_argv_contains": "node",
        "anchor_flag": "--x=",
        "id_template": "x-{worker}-{0}",
        "worker": {"required": True},
    }
    with pytest.raises(cleanup_rules.RuleError):
        cleanup_rules._validate_kill_family(family)


# ── cleanup_rules.resolve_clean_log_paths ────────────────────────────────────────────────────


def test_resolve_clean_log_paths_accepts_allowed_root_and_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_path,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_path)], "allowed_suffixes": [".log"]}]}
    resolved = cleanup_rules.resolve_clean_log_paths(rules, [str(tmp_path / "a.log")])
    assert resolved == [tmp_path / "a.log"]


def test_resolve_clean_log_paths_rejects_wrong_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_path,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_path)], "allowed_suffixes": [".log"]}]}
    with pytest.raises(cleanup_rules.RuleError, match="does not match any clean_log_rules"):
        cleanup_rules.resolve_clean_log_paths(rules, [str(tmp_path / "a.txt")])


def test_resolve_clean_log_paths_rejects_path_outside_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_path / "only-this",))
    (tmp_path / "only-this").mkdir()
    outside = tmp_path / "elsewhere.log"
    rules = {
        "clean_log_rules": [
            {"allowed_roots": [str(tmp_path / "only-this")], "allowed_suffixes": [".log"]}
        ]
    }
    with pytest.raises(cleanup_rules.RuleError, match="does not match any clean_log_rules"):
        cleanup_rules.resolve_clean_log_paths(rules, [str(outside)])


def test_resolve_clean_log_paths_rejects_glob():
    with pytest.raises(cleanup_rules.RuleError, match="glob patterns are not supported"):
        cleanup_rules.resolve_clean_log_paths(_SAMPLE_RULES, ["/tmp/*.log"])


def test_resolve_clean_log_paths_rejects_too_many_paths():
    paths = [f"/tmp/x{i}.log" for i in range(21)]
    with pytest.raises(cleanup_rules.RuleError, match="exceeds the 20"):
        cleanup_rules.resolve_clean_log_paths(_SAMPLE_RULES, paths)


def test_resolve_clean_log_paths_no_rules_configured_raises():
    with pytest.raises(cleanup_rules.RuleError, match="no clean_log_rules"):
        cleanup_rules.resolve_clean_log_paths({}, ["/tmp/a.log"])


def test_resolve_clean_log_paths_rejects_root_outside_safe_temp_roots():
    """An allowed_roots entry pointing outside the fixed safe-temp-root allow-list is treated
    as matching nothing, not silently honored (rules.yaml's ${TMPDIR}-style entries are the
    exact hazard this guards: an env-derived / attacker-influenced root)."""
    rules = {"clean_log_rules": [{"allowed_roots": ["/etc"], "allowed_suffixes": [".log"]}]}
    with pytest.raises(cleanup_rules.RuleError, match="does not match any clean_log_rules"):
        cleanup_rules.resolve_clean_log_paths(rules, ["/etc/anything.log"])


def test_log_path_matches_rule_rejects_bare_root_itself(tmp_path, monkeypatch):
    """A target path equal to the allowed root itself is never a match, even when the root's
    own name ends in an allowed suffix (e.g. a root literally named `.../weird.log/`) — so the
    suffix check alone can't be what's rejecting it; this exercises the `resolved == root`
    guard specifically."""
    root = tmp_path / "weird.log"
    root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (root,))
    rule = {"allowed_roots": [str(root)], "allowed_suffixes": [".log"]}
    assert cleanup_rules._log_path_matches_rule(root, rule) is False


def test_resolve_safe_root_rejects_non_temp_path():
    assert cleanup_rules._resolve_safe_root("/etc") is None
    assert cleanup_rules._resolve_safe_root(str(Path.home())) is None


def test_resolve_safe_root_accepts_tmp_and_var_folders():
    assert cleanup_rules._resolve_safe_root("/tmp") is not None
    assert cleanup_rules._resolve_safe_root("/private/tmp") is not None
    assert cleanup_rules._resolve_safe_root("/var/folders/xy/z") is not None


# ── _validate_clean_log_rule ─────────────────────────────────────────────────────────────────


def test_validate_clean_log_rule_rejects_missing_allowed_roots():
    with pytest.raises(cleanup_rules.RuleError, match="allowed_roots"):
        cleanup_rules._validate_clean_log_rule({"allowed_suffixes": [".log"]})


def test_validate_clean_log_rule_rejects_missing_allowed_suffixes():
    with pytest.raises(cleanup_rules.RuleError, match="allowed_suffixes"):
        cleanup_rules._validate_clean_log_rule({"allowed_roots": ["/tmp"]})


# ── dev kill-orphan CLI wiring ────────────────────────────────────────────────────────────────


def test_cmd_kill_orphan_kills_only_matching_process(wired_cli, monkeypatch):
    sent: list[tuple] = []
    matched_command = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"

    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_command", lambda pid: matched_command)
    monkeypatch.setattr(
        cli,
        "_list_all_processes",
        lambda: [
            (111, matched_command),
            (222, "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-0-deadbeef"),
            (333, "/usr/bin/some-other-tool"),
        ],
    )

    exit_code = cli.main(
        [
            "kill-orphan",
            "--harness",
            "vscode-e2e-harness",
            "--worker",
            "2",
            "--instance",
            "19fd16aa",
        ]
    )
    assert exit_code == 0
    assert sent == [(111, signal.SIGKILL)]


def test_cmd_kill_orphan_no_match_is_a_noop(wired_cli, monkeypatch, capsys):
    sent: list[tuple] = []
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [])

    exit_code = cli.main(["kill-orphan", "--harness", "vscode-e2e-harness", "--worker", "5"])
    assert exit_code == 0
    assert sent == []
    assert "no processes matched" in capsys.readouterr().out


def test_cmd_kill_orphan_exceeding_max_matches_refuses(wired_cli, monkeypatch):
    rules = copy.deepcopy(_SAMPLE_RULES)
    rules["kill_families"][0]["max_matches"] = 1
    _write_rules(wired_cli["tmp_path"], rules)
    monkeypatch.setattr(cleanup_rules, "DEFAULT_RULES_PATH", wired_cli["rules_path"])

    sent: list[tuple] = []
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(
        cli,
        "_list_all_processes",
        lambda: [
            (111, "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"),
            (112, "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-aaaaaaaa"),
        ],
    )

    exit_code = cli.main(["kill-orphan", "--harness", "vscode-e2e-harness", "--worker", "2"])
    assert exit_code == cli.EXIT_USAGE
    assert sent == []  # refused before sending any signal


def test_cmd_kill_orphan_signal_failure_exits_nonzero(wired_cli, monkeypatch):
    """A real signal failure (e.g. permission denied) must exit nonzero, matching
    dev clean-logs' behavior for a real deletion failure — a caller checking exit codes needs
    a programmatic signal that a kill actually failed, not just a printed line."""
    matched_command = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"

    def _send_signal_denied(pid, sig):
        raise cli.DevError(f"permission denied sending signal to pid {pid}")

    monkeypatch.setattr(cli, "_send_signal", _send_signal_denied)
    monkeypatch.setattr(cli, "_process_command", lambda pid: matched_command)
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [(111, matched_command)])

    exit_code = cli.main(["kill-orphan", "--harness", "vscode-e2e-harness", "--worker", "2"])
    assert exit_code == cli.EXIT_USAGE
    records = _read_audit_records(wired_cli["state_dir"])
    assert records[1]["failed_pids"] == [111]
    assert records[1]["killed_pids"] == []


def test_cmd_kill_orphan_sigterm_then_sigkill_escalates_after_grace(wired_cli, monkeypatch):
    sent: list[tuple] = []
    slept: list[float] = []
    matched_command = "fake-server --tag=srv-0-cafecafe"

    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_command", lambda pid: matched_command)
    monkeypatch.setattr(cli.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [(444, matched_command)])

    exit_code = cli.main(
        ["kill-orphan", "--harness", "graceful-server", "--worker", "0", "--instance", "cafecafe"]
    )
    assert exit_code == 0
    assert sent == [(444, signal.SIGTERM), (444, signal.SIGKILL)]
    assert slept == [5.0]


def test_cmd_kill_orphan_skips_escalation_if_pid_died_during_grace(wired_cli, monkeypatch):
    """If the target already died from the initial SIGTERM (confirmed dead by the post-grace
    liveness check), the SIGKILL escalation must not fire."""
    sent: list[tuple] = []
    matched_command = "fake-server --tag=srv-0-cafecafe"

    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    # _pid_alive is consulted only once per pid, as the post-grace escalation gate — False
    # here means "already dead, don't escalate."
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(cli, "_process_command", lambda pid: matched_command)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [(444, matched_command)])

    exit_code = cli.main(
        ["kill-orphan", "--harness", "graceful-server", "--worker", "0", "--instance", "cafecafe"]
    )
    assert exit_code == 0
    assert sent == [(444, signal.SIGTERM)]  # no SIGKILL


def test_cmd_kill_orphan_skips_signal_if_command_changed_before_signaling(wired_cli, monkeypatch):
    """A pid whose command line no longer matches at signal time (reused by an unrelated
    process between enumeration and signaling) must not be signaled."""
    sent: list[tuple] = []
    matched_command = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"

    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_command", lambda pid: "/usr/bin/completely-unrelated")
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [(111, matched_command)])

    exit_code = cli.main(["kill-orphan", "--harness", "vscode-e2e-harness", "--worker", "2"])
    assert exit_code == 0
    assert sent == []


def test_cmd_kill_orphan_writes_audit_log_before_and_after(wired_cli, monkeypatch):
    matched_command = "code --extensionDevelopmentPath=/x --user-data-dir=/tmp/hvsc-2-19fd16aa"
    monkeypatch.setattr(cli, "_send_signal", lambda pid, sig: None)
    monkeypatch.setattr(cli, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(cli, "_process_command", lambda pid: matched_command)
    monkeypatch.setattr(cli, "_list_all_processes", lambda: [(111, matched_command)])

    assert cli.main(["kill-orphan", "--harness", "vscode-e2e-harness", "--worker", "2"]) == 0
    records = _read_audit_records(wired_cli["state_dir"])
    assert [r["action"] for r in records] == ["kill-orphan", "kill-orphan-result"]
    assert records[0]["intended"] == [{"pid": 111, "command": matched_command}]
    assert records[1]["killed_pids"] == [111]
    assert records[1]["already_gone_pids"] == []
    assert records[1]["failed_pids"] == []


def test_cmd_kill_orphan_unknown_harness_exits_usage(wired_cli):
    exit_code = cli.main(["kill-orphan", "--harness", "nope", "--worker", "0"])
    assert exit_code == cli.EXIT_USAGE


# ── dev clean-logs CLI wiring ─────────────────────────────────────────────────────────────────


def test_cmd_clean_logs_deletes_matching_files(wired_cli, monkeypatch):
    tmp_root = wired_cli["tmp_path"] / "fake-tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_root,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_root)], "allowed_suffixes": [".log"]}]}
    _write_rules(wired_cli["tmp_path"], rules)
    log_file = tmp_root / "run.log"
    log_file.write_text("data", encoding="utf-8")

    exit_code = cli.main(["clean-logs", str(log_file)])
    assert exit_code == 0
    assert not log_file.exists()


def test_cmd_clean_logs_rejects_disallowed_path_without_deleting(wired_cli, monkeypatch):
    tmp_root = wired_cli["tmp_path"] / "fake-tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_root,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_root)], "allowed_suffixes": [".log"]}]}
    _write_rules(wired_cli["tmp_path"], rules)

    other_root = wired_cli["tmp_path"] / "elsewhere"
    other_root.mkdir()
    victim = other_root / "important.log"
    victim.write_text("do not delete me", encoding="utf-8")

    exit_code = cli.main(["clean-logs", str(victim)])
    assert exit_code == cli.EXIT_USAGE
    assert victim.exists()


def test_cmd_clean_logs_multiple_paths_all_validated_before_any_delete(wired_cli, monkeypatch):
    """One bad path in a multi-path call must not delete the good ones first."""
    tmp_root = wired_cli["tmp_path"] / "fake-tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_root,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_root)], "allowed_suffixes": [".log"]}]}
    _write_rules(wired_cli["tmp_path"], rules)

    good = tmp_root / "good.log"
    good.write_text("data", encoding="utf-8")
    bad = tmp_root / "bad.txt"
    bad.write_text("data", encoding="utf-8")

    exit_code = cli.main(["clean-logs", str(good), str(bad)])
    assert exit_code == cli.EXIT_USAGE
    assert good.exists()  # not deleted, because validation of the whole batch failed first
    assert bad.exists()


def test_cmd_clean_logs_writes_audit_log_before_and_after(wired_cli, monkeypatch):
    tmp_root = wired_cli["tmp_path"] / "fake-tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_root,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_root)], "allowed_suffixes": [".log"]}]}
    _write_rules(wired_cli["tmp_path"], rules)
    log_file = tmp_root / "run.log"
    log_file.write_text("data", encoding="utf-8")

    assert cli.main(["clean-logs", str(log_file)]) == 0
    records = _read_audit_records(wired_cli["state_dir"])
    assert [r["action"] for r in records] == ["clean-logs", "clean-logs-result"]
    assert records[0]["intended_paths"] == [str(log_file)]
    assert records[1]["removed"] == [str(log_file)]


def test_cmd_clean_logs_reports_and_exits_nonzero_on_os_error(wired_cli, monkeypatch):
    tmp_root = wired_cli["tmp_path"] / "fake-tmp"
    tmp_root.mkdir()
    monkeypatch.setattr(cleanup_rules, "_SAFE_TEMP_ROOTS", (tmp_root,))
    rules = {"clean_log_rules": [{"allowed_roots": [str(tmp_root)], "allowed_suffixes": [".log"]}]}
    _write_rules(wired_cli["tmp_path"], rules)
    log_file = tmp_root / "run.log"
    log_file.write_text("data", encoding="utf-8")

    def _boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "unlink", _boom)

    exit_code = cli.main(["clean-logs", str(log_file)])
    assert exit_code == cli.EXIT_USAGE
    records = _read_audit_records(wired_cli["state_dir"])
    assert records[1]["failed"]  # the attempt is still recorded despite the failure
