"""install-skill — register the ``dev`` agent skill so harnesses auto-discover it.

WHAT this does: brings ``dev install-skill`` to PARITY with the sibling personal CLIs
(draw/tg/review/task/research), whose installers each write THREE idempotent layers — not just
a SKILL.md:

1. **SKILL.md + blurb + compat symlink.** ``~/.agents/skills/dev/SKILL.md`` (Agent Skills
   standard) so Claude Code / Codex / opencode / Gemini / Cursor surface ``dev`` as a
   capability, plus the one-line ``~/.agents/skills/.blurbs/dev.md`` (the always-on
   advertisement a SessionStart hook cats into every session), plus a
   ``~/.claude/skills/dev`` symlink (Claude Code also scans that dir).
2. **A marked block in each detected harness's instruction file.** A
   ``<!-- skill:dev -->…<!-- /skill:dev -->`` block is written into the instruction file of
   each harness DETECTED on this machine. The block is REPLACED on re-run (never duplicated);
   nothing else in the file is touched.
3. **An idempotent SessionStart aggregator hook** in ``~/.claude/settings.json`` that cats every
   ``.blurbs/*.md`` into each new Claude Code session.

Stdlib-only; every write is idempotent (skips an already-current target, replaces — never
duplicates — a marked block, and won't re-add an existing hook). The block/hook plumbing mirrors
the sibling installers byte-for-byte so a machine advertises ``dev`` exactly where it advertises
draw/tg/review/task/research.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SKILL_NAME = "dev"
SKILL_MD = """\
---
name: dev
description: >-
  Project-scoped dev/e2e process runner. Runs a repo's `rig.yaml` scripts and manages
  background dev servers / e2e jobs, per project. Use INSTEAD of raw `pkill`/`lsof`/`ps` for
  dev-process cleanup and instead of hand-rolled test/e2e invocation. Commands:
  `dev start|list|status|logs|run <script>|has-script|stop|e2e|env`. `dev stop` is
  safe-by-default — it SIGTERMs only a known dev/e2e runner scoped to the current repo (or
  `DEV_PROJECT_PATHS`). Exit 2 = invalid args/unsafe stop target; 127 = missing platform helper.
metadata:
  author: alex-mextner
  repo: https://github.com/alex-mextner/dev-cli
---

# dev — project-scoped dev/e2e process runner

A small project-scoped front door for the development actions agents run constantly, without
widening permission to arbitrary shell or process control. It covers named `rig.yaml` scripts
plus configured dev-server and e2e-job lifecycle commands, and it validates every command shape
and stop target before acting.

## Commands
```
dev run [--repo-only] <script> [-- <args>...]   # run a named top-level rig.yaml scripts:<name>
dev has-script [--repo-only] <script>            # quiet existence check (exit 0 present / 1 absent)
dev start <target> [-- <args>...]                # start a configured dev.server / dev.e2e job
dev list                                          # configured + running/stale targets
dev status [target]                               # configured/running/stale + latest artifacts
dev logs <target> [--tail N]                      # tail a configured target log
dev e2e run|status|logs|stop <target>             # first-class e2e-job aliases
dev stop <target> | --pid | --port | --pgid       # validated SIGTERM (dev tool + project-scoped)
dev env --add-project <path>                      # print an export line for a multi-project session
dev --help                                        # full usage
```

## Key facts
- **Safe-by-default `dev stop`**: it only SIGTERMs a process that (a) looks like a dev/e2e tool
  and (b) is scoped to the current repo or `DEV_PROJECT_PATHS`. Prefer it over raw
  `pkill`/`kill`/`lsof` for dev-process cleanup.
- **Command-shape validation**: destructive heads (`rm`, `git reset`, `docker system prune`, …),
  inline redirection, and command substitution are refused in `dev run`/`dev start` — put complex
  shell in a reviewed project-local wrapper script.
- **stdlib-only at import**; reading `rig.yaml` loads the (vendored) `agenttools_config` + PyYAML
  lazily, so `dev --help` stays fast and dependency-free.
- **Exit codes**: `0` ok, script exit for `dev run`, `2` invalid args/config/unsafe stop target,
  `127` a required platform helper (lsof/ps) is missing.
"""

# One-line SessionStart blurb, same shape the siblings (draw/tg/review/task/research) use. The
# hook cats every ``.blurbs/*.md`` verbatim, so keep this a single bullet line ending in a newline.
BLURB = (
    "- `dev` — project-scoped dev/e2e process runner. Runs a repo's `rig.yaml` scripts and manages "
    "background dev servers / e2e jobs, per project: `dev start|list|status|logs|run <script>|"
    "has-script|stop|e2e|env`. `dev stop` is safe-by-default (SIGTERMs only a known dev/e2e runner "
    "scoped to the current repo or `DEV_PROJECT_PATHS`) — use it instead of raw `pkill`/`lsof` for "
    "dev-process cleanup. Exit 2 = invalid args/unsafe stop target; 127 = missing platform helper.\n"
)


# The SessionStart aggregator that cats every installed tool's blurb into a new session. The
# marker (a trailing shell comment) makes the hook self-identifying so re-installs never add a
# second copy; it matches the shape rig + the sibling installers already use on this machine.
_HOOK_MARKER = "# agent-tools-awareness"
_HOOK_COMMAND = (
    'sh -c \'d="$HOME/.agents/skills/.blurbs"; ls "$d"/*.md >/dev/null 2>&1 && '
    '{ printf "Agent CLI tools installed on this machine (prefer them):\\n"; '
    'cat "$d"/*.md; }\' ' + _HOOK_MARKER
)

# The harness instruction files this installer injects the marked blurb-block into — but ONLY
# when the harness is detected on this machine (its config dir exists). Keep in step with the
# siblings (tg/review/draw/task/research) so a machine advertises `dev` exactly where it does them.
_HARNESSES: tuple[tuple[str, str], ...] = (
    (".claude", "CLAUDE.md"),
    (".codex", "AGENTS.md"),
    (".config/opencode", "AGENTS.md"),
    (".gemini", "GEMINI.md"),
)


def _home() -> Path:
    return Path(os.environ.get("HOME") or os.path.expanduser("~"))


def _write_if_changed(target: Path, content: str) -> bool:
    """Write ``content`` to ``target`` unless it is already current. Returns True on write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_text(encoding="utf-8") == content:
        print(f"dev: already current at {target}")
        return False
    target.write_text(content, encoding="utf-8")
    print(f"dev: wrote {target}")
    return True


def _ensure_claude_skills_symlink(home: Path) -> None:
    """Symlink ``~/.claude/skills/dev`` → the canonical skill dir (Claude Code scans both).

    Only when ``~/.claude/skills`` already exists. A pre-existing link/file is left as-is; a
    symlink failure (unsupported FS / race) is non-fatal — the canonical ``~/.agents`` copy is
    what matters.
    """
    claude_skills = home / ".claude" / "skills"
    if not claude_skills.is_dir():
        return
    link = claude_skills / SKILL_NAME
    if link.exists() or link.is_symlink():
        return
    try:
        link.symlink_to(home / ".agents" / "skills" / SKILL_NAME)
        print(f"dev: linked {link}")
    except OSError:
        pass  # symlink unsupported or a race — the ~/.agents copy still advertises the skill


def _blurb_block(blurb: str) -> str:
    """The marked block written into a harness instruction file (replaced wholesale on re-run)."""
    return f"<!-- skill:{SKILL_NAME} -->\n{blurb.rstrip()}\n<!-- /skill:{SKILL_NAME} -->\n"


def _inject_marked_block(path: Path, blurb: str) -> None:
    """Insert (or refresh IN PLACE) the ``<!-- skill:dev -->…`` block in ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    new_text = _replace_or_append_block(existing, _blurb_block(blurb))
    if path.is_file() and existing == new_text:
        print(f"dev: already current at {path}")
        return
    path.write_text(new_text, encoding="utf-8")
    print(f"dev: advertised in {path}")


def _replace_or_append_block(existing: str, block: str) -> str:
    """Return ``existing`` with our marked block refreshed in place, or appended if absent.

    Pure (no I/O) so the order-preserving behavior is unit-testable. When a well-formed
    ``<!-- skill:dev -->…<!-- /skill:dev -->`` pair exists, ONLY that exact pair is swapped for
    ``block`` at the same offset; everything around it is preserved. We anchor on the FIRST close
    marker and the LAST open marker BEFORE it, so a stray earlier open marker is treated as an
    orphan (stripped, surrounding text kept), not as the block's opening.
    """
    start, end = f"<!-- skill:{SKILL_NAME} -->", f"<!-- /skill:{SKILL_NAME} -->"
    e_idx = existing.find(end)
    s_idx = existing.rfind(start, 0, e_idx) if e_idx != -1 else -1
    if e_idx != -1 and s_idx != -1:
        block_end = e_idx + len(end)
        if block_end < len(existing) and existing[block_end] == "\n":
            block_end += 1  # consume the block's own trailing newline so we don't double it
        rebuilt = existing[:s_idx] + block + existing[block_end:]
        return _strip_stray_markers(rebuilt, (start, end), protect=(s_idx, s_idx + len(block)))
    existing = _strip_stray_markers(existing, (start, end), protect=None)
    body = existing.rstrip()
    return f"{body}\n\n{block}" if body else block


def _strip_stray_markers(
    text: str, markers: tuple[str, ...], *, protect: tuple[int, int] | None
) -> str:
    """Remove every stray skill-marker token (each of ``markers``) from ``text``, keeping all other
    text. Stripping BOTH the open and close markers symmetrically keeps the refresh idempotent.

    ``protect`` is a ``(lo, hi)`` byte span (the freshly-inserted block) whose markers are left
    intact. Only the token is removed (not its line), so user text sharing a line with a stray
    marker survives.
    """
    spans: list[tuple[int, int]] = []
    for marker in markers:
        i = 0
        while True:
            j = text.find(marker, i)
            if j == -1:
                break
            spans.append((j, j + len(marker)))
            i = j + len(marker)
    out: list[str] = []
    cursor = 0
    for lo, hi in sorted(spans):
        if lo < cursor:
            continue  # overlapping match already consumed
        if protect is not None and protect[0] <= lo < protect[1]:
            continue  # keep the protected (real-block) marker
        out.append(text[cursor:lo])  # keep text before the stray token, drop the token itself
        cursor = hi
    out.append(text[cursor:])
    return "".join(out)


def _inject_into_detected_harnesses(home: Path, blurb: str) -> None:
    """Inject the marked blurb-block into each detected harness's instruction file."""
    for config_dir, instruction_file in _HARNESSES:
        if (home / config_dir).is_dir():
            _inject_marked_block(home / config_dir / instruction_file, blurb)


def _hook_already_present(session_start: list) -> bool:
    """True if a SessionStart hook carrying our marker is already registered."""
    for group in session_start:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            cmd = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(cmd, str) and _HOOK_MARKER in cmd:
                return True
    return False


def _ensure_session_start_hook(home: Path) -> None:
    """Add the SessionStart blurb-aggregator hook to ``~/.claude/settings.json``, idempotently.

    Conservative by design: only when ``~/.claude`` exists; a missing settings file is created
    with just the hook; an UNPARSEABLE settings file is left untouched (never clobbered); and an
    already-present hook (matched by its marker) is a no-op. We back up the file before rewriting.
    """
    if not (home / ".claude").is_dir():
        return
    settings_path = home / ".claude" / "settings.json"
    original = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
    try:
        data = json.loads(original) if original is not None else {}
    except ValueError:
        print(f"dev: WARNING — {settings_path} is not valid JSON; skipping the SessionStart hook")
        return
    if not isinstance(data, dict):
        print(f"dev: WARNING — {settings_path} is not a JSON object; skipping the SessionStart hook")
        return

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        print(f"dev: WARNING — {settings_path} has a non-object 'hooks'; skipping the hook")
        return
    session_start = hooks.setdefault("SessionStart", [])
    if not isinstance(session_start, list):
        print(f"dev: WARNING — {settings_path} has a non-list 'SessionStart'; skipping the hook")
        return
    if _hook_already_present(session_start):
        print(f"dev: SessionStart hook already present in {settings_path}")
        return

    session_start.append({"hooks": [{"type": "command", "command": _HOOK_COMMAND}]})
    if original is not None:
        backup = settings_path.parent / "settings.json.bak"
        if not (backup.exists() or backup.is_symlink()):
            backup.write_text(original, encoding="utf-8")
    _atomic_write(settings_path, json.dumps(data, indent=2) + "\n")
    print(f"dev: added SessionStart blurb-aggregator hook to {settings_path}")


def _atomic_write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` via a temp file + rename, so a crash can't truncate it."""
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)


def install_skill() -> int:
    """Install all three advertisement layers (idempotent). See the module docstring."""
    home = _home()
    skills_root = home / ".agents" / "skills"

    # Layer 1 — SKILL.md + always-on blurb + the Claude-Code compat symlink.
    _write_if_changed(skills_root / SKILL_NAME / "SKILL.md", SKILL_MD)
    _write_if_changed(skills_root / ".blurbs" / f"{SKILL_NAME}.md", BLURB)
    _ensure_claude_skills_symlink(home)

    # Layer 2 — a marked blurb-block in each detected harness instruction file.
    _inject_into_detected_harnesses(home, BLURB)

    # Layer 3 — the SessionStart aggregator hook (Claude Code).
    _ensure_session_start_hook(home)
    return 0
