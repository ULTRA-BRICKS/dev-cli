#!/usr/bin/env python3
"""pre-commit gate: a commit that touches rules.yaml must carry a passed review-cli quorum.

Accessed via: dev-cli's own git hooks (``git config core.hooksPath scripts/git-hooks``, set by
``install.sh`` for a checkout of THIS repo — not the ``dev`` tool's own runtime, which never
runs this file). Wired as ``scripts/git-hooks/pre-commit``, which calls this script.

Why: ``rules.yaml`` — and ``dev_cli/cleanup_rules.py``'s validation/selection functions, an
EQUAL safety boundary — govern whether ``dev kill-orphan`` / ``dev clean-logs`` can express an
unscoped process kill or file delete (see that module's docstring). Loosening a pattern there
— widening a regex, dropping an anchor, adding a family with no anchor_flag — is a real
safety-boundary change, not a style edit, so COMMITTING it needs the same kind of independent
verification the rest of this ecosystem requires before a self-merge (mirrors agent-tools'
``ci/ship/ship.sh`` Guard-B: >=3 PASSED review-cli iterations for the task code, checked via
``review task <code> --check``). This hook does the SAME check locally, at both ``pre-commit``
and ``pre-merge-commit``.

Design: this script does NOT read or trust a hand-written "reviewed: yes" marker anywhere. It
shells out to ``review task <code> --check -C <repo>`` on every commit that touches a gated
file, which re-derives the answer from review-cli's own iteration-history store at that
moment. There is nothing to forge in the diff itself — the enforcement lives entirely in the
live check.

Scope — read this before trusting the guarantee further than it goes: this hook gates COMMITS
to the git history of a checkout that has run install.sh, nothing more. It does NOT prevent a
live, uncommitted edit to rules.yaml/cleanup_rules.py from taking effect immediately (dev
reads the working tree, not git history) — see AGENTS.md's "Known limits" note for the full
list (fast-forward merges, an old already-passed task code on a fresh branch, an un-installed
clone). Treat it as raising the bar for an accidental or drive-by loosening, not as a runtime
sandbox or an unforgeable guarantee.

Escape hatch: none built in on purpose. If review-cli is unreachable, this refuses (fail-closed)
with a clear message; the operator can still bypass with a bare ``git commit --no-verify``, but
that is git's own universal hook bypass, not a hole this script leaves open on purpose.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

RULES_FILE = "rules.yaml"
# dev_cli/cleanup_rules.py's validation functions are an EQUAL safety boundary to rules.yaml
# itself — relaxing what counts as a valid anchor_flag/id_template there is just as much a
# loosening as editing the YAML. Gate on either changing.
INTERPRETER_FILE = "dev_cli/cleanup_rules.py"
_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]*-[0-9]+)\b")


def main() -> int:
    repo_root = _repo_root()
    if repo_root is None:
        return 0  # not inside a git repo; nothing to gate
    gated_file = _touched_gated_file(repo_root)
    if gated_file is None:
        return 0

    task_code = _find_task_code(repo_root)
    if not task_code:
        _fail(
            f"this commit touches {gated_file} but no task/ticket code (e.g. HYP-123) was "
            f"found on the current branch name. rules.yaml/{INTERPRETER_FILE} edits require "
            "a review-cli quorum tied to a task code — rename/create the branch with the "
            "code in its name, get it reviewed (`review diff -C . --task <CODE>`), then "
            "commit again."
        )
        return 1

    review_bin = shutil.which("review")
    if review_bin is None:
        _fail(
            f"review-cli ('review' on PATH) is required to verify the quorum for a {gated_file} "
            "change and was not found. Install review-cli, or run "
            "`review diff -C . --task <CODE>` to record iterations first."
        )
        return 1

    result = subprocess.run(
        [review_bin, "task", task_code, "--check", "-C", str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        print(f"check_rules_quorum: {gated_file} change OK — quorum met for {task_code}")
        return 0

    detail = (result.stdout + result.stderr).strip()
    _fail(
        f"{gated_file} is staged for commit but task {task_code} has not met review-cli's "
        f"quorum bar yet. Run `review diff -C . --task {task_code}` (repeat across "
        f"independent passes/models) until `review task {task_code} --check -C .` exits 0, "
        f"then commit again.\n\nreview task output:\n{detail}"
    )
    return 1


def _repo_root() -> Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _touched_gated_file(repo_root: Path) -> str | None:
    """Return the first gated file (RULES_FILE or INTERPRETER_FILE) present in the staged
    diff, or None if neither is staged. Both are equal safety boundaries — see the module
    docstring and the INTERPRETER_FILE comment above."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    staged = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    for candidate in (RULES_FILE, INTERPRETER_FILE):
        if candidate in staged:
            return candidate
    return None


def _find_task_code(repo_root: Path) -> str | None:
    """Return the task code from the CURRENT branch name, or None.

    Deliberately does NOT fall back to scanning HEAD's commit message. A prior version did,
    to cover a `git commit --amend` flow — but on a branch whose name carries no code (e.g.
    `main`, or a merge target), HEAD is whatever commit was there before this one, which can
    be completely unrelated work with its own, already-quorum-passed task code. That fallback
    would let a rules.yaml loosening on such a branch pass by riding on an old, unrelated
    task's history — the exact kind of gap this gate exists to close. An amend flow keeps its
    branch name across the amend, so the branch-name check alone still covers it.
    """
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    branch = branch_result.stdout.strip()
    match = _TICKET_RE.search(branch.upper())
    return match.group(1) if match else None


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
