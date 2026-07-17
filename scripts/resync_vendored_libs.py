#!/usr/bin/env python3
"""Re-sync the vendored agent-tools libraries from the agent-tools canonical source.

WHAT this does: rebuilds the vendored copies under ``vendor/`` so each is byte-identical to
the current canonical file in agent-tools, and refreshes the pinned ``CANONICAL_SHA256`` +
``CANONICAL_AGENT_TOOLS_COMMIT`` recorded in each vendored copy's SYNC-HEADER block.

WHY it exists: dev-cli was spun out of the agent-tools umbrella (it lived at
``lib/agenttools_dev``). It HARD-depends on the shared ``agenttools_config`` loader to read the
``rig.yaml`` cascade. That lib is not on PyPI (a publish needs the CTO's account), so this repo
VENDORS it — strategy B, the proven research-cli/task-cli pattern: a vendored copy + a pinned-SHA
drift guard that keeps single-source-of-truth via a check while making the repo self-contained.

The vendored copy drifts the moment agent-tools changes the canonical.
``tests/test_vendored_libs_sync.py`` fails CI when a vendored body no longer matches its pinned
SHA; this script is the documented, one-command way to make it match again after an intentional
canonical change.

HOW to run::

    python scripts/resync_vendored_libs.py /path/to/agent-tools           # re-sync the bodies
    python scripts/resync_vendored_libs.py --check /path/to/agent-tools   # drift check (CI)

``--check`` compares only the canonical BODY hash (never ``CANONICAL_AGENT_TOOLS_COMMIT``,
which a shallow CI checkout can't resolve) and keys its exit code: 0 == in sync, 1 == real
DRIFT (re-sync), >=2 == infra error (path moved / usage / crash — NOT drift).

INVARIANT: the SYNC-HEADER block is the ONLY delta between a vendored copy and its canonical
source. The header is bounded by ``# SYNC-HEADER-BEGIN`` / ``# SYNC-HEADER-END`` (a comment in
both Python and Markdown) so the guard strips exactly it and compares the remainder; do not add
content outside that block.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple

SYNC_BEGIN = "# SYNC-HEADER-BEGIN"
SYNC_END = "# SYNC-HEADER-END"


class VendoredItem(NamedTuple):
    """One vendored file: its path in an agent-tools checkout and its path in this repo."""

    canonical_rel: Path  # relative to the agent-tools checkout root
    vendored_rel: Path  # relative to this repo root


# The agent-tools files this repo vendors, and where each lands under vendor/. The Python
# package keeps its package layout (vendor/<pkg>/...) so it imports as `agenttools_config`.
VENDORED: List[VendoredItem] = [
    VendoredItem(
        Path("lib/agenttools_config/__init__.py"),
        Path("vendor/agenttools_config/__init__.py"),
    ),
    VendoredItem(
        Path("lib/agenttools_config/core.py"),
        Path("vendor/agenttools_config/core.py"),
    ),
    VendoredItem(
        Path("lib/agenttools_config/README.md"),
        Path("vendor/agenttools_config/README.md"),
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent


def canonical_commit(canonical: Path) -> str:
    """The agent-tools commit that last touched ``canonical`` (provenance for the header)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(canonical.parent), "log", "-1", "--format=%H", "--", canonical.name],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def build_sync_header(item: VendoredItem, canonical_sha: str, canonical_commit_sha: str) -> str:
    """The SYNC-HEADER block (bounded by the BEGIN/END markers the guard strips).

    A ``#`` comment is valid in BOTH Python and Markdown-as-leading-comment, so the same header
    shape works for every vendored file (the .md file carries it as a leading comment block too).
    """
    return "\n".join(
        [
            f"{SYNC_BEGIN}  (this block is the ONLY delta from the canonical source; the drift",
            "#   guard strips exactly these lines and asserts the remainder is byte-identical to the hash)",
            f"# VENDORED COPY of agent-tools/{item.canonical_rel.as_posix()}.",
            "# The canonical source lives in agent-tools (rig provisions it onto a real machine). dev-cli",
            "# was spun out of that umbrella (it lived at lib/agenttools_dev) and VENDORS the shared config",
            "# loader so this repo is self-contained — strategy B, the research-cli/task-cli pattern.",
            "#",
            "# DRIFT GUARD: tests/test_vendored_libs_sync.py reconstructs the canonical content from this file",
            "# (the SYNC-HEADER block removed) and asserts its SHA256 equals CANONICAL_SHA256 below. A local",
            "# edit to this copy, OR a stale copy after the canonical changes upstream, fails CI instead of",
            "# silently diverging.",
            "#",
            f"# CANONICAL_SHA256: {canonical_sha}",
            f"# CANONICAL_AGENT_TOOLS_COMMIT: {canonical_commit_sha}",
            "#",
            "# TO RE-SYNC after the canonical changes: run",
            "#   python scripts/resync_vendored_libs.py <path-to-agent-tools>",
            "# (re-copies the canonical body and refreshes CANONICAL_SHA256 + the commit above).",
            SYNC_END,
        ]
    )


def render_vendored(
    item: VendoredItem, canonical_text: str, canonical_sha: str, canonical_commit_sha: str
) -> str:
    """Render a vendored file: SYNC-HEADER block on top + the canonical body verbatim.

    The header goes at the very TOP (these canonical files carry no shebang), so the body that
    follows is byte-identical to the canonical and the guard recovers it by stripping the block.
    """
    header = build_sync_header(item, canonical_sha, canonical_commit_sha)
    return f"{header}\n{canonical_text}"


def reconstruct_canonical_from_vendored(vendored_text: str) -> str:
    """Strip the ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block to recover the canonical content.

    The block is the ONLY delta from the canonical source, so removing it yields the canonical
    text byte-for-byte. Kept identical to the stripping in tests/test_vendored_libs_sync.py.
    """
    out: List[str] = []
    skipping = False
    for line in vendored_text.split("\n"):
        if line.startswith(SYNC_BEGIN):
            skipping = True
            continue
        if line.startswith(SYNC_END):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_one(item: VendoredItem, canonical: Path) -> int:
    """--check a single vendored item against its canonical body. Returns an rc per the contract."""
    vendored = REPO_ROOT / item.vendored_rel
    if not vendored.is_file():
        print(
            f"error: vendored copy missing at {vendored} (repo breakage, not drift)",
            file=sys.stderr,
        )
        return 2
    canonical_sha = _sha256(canonical.read_text(encoding="utf-8"))
    vendored_body_sha = _sha256(
        reconstruct_canonical_from_vendored(vendored.read_text(encoding="utf-8"))
    )
    if vendored_body_sha == canonical_sha:
        print(f"OK: {item.vendored_rel.as_posix()} in sync (sha256 {canonical_sha})")
        return 0
    print(
        f"DRIFT: {item.vendored_rel.as_posix()} no longer matches the canonical source.\n"
        f"  vendored body sha256 = {vendored_body_sha}\n"
        f"  canonical sha256     = {canonical_sha}\n"
        "  re-run without --check to re-sync, then commit.",
        file=sys.stderr,
    )
    return 1


def _resync_one(item: VendoredItem, canonical: Path) -> None:
    """Re-sync a single vendored item: re-copy the canonical body + refresh the pinned SHA."""
    canonical_text = canonical.read_text(encoding="utf-8")
    canonical_sha = _sha256(canonical_text)
    rendered = render_vendored(item, canonical_text, canonical_sha, canonical_commit(canonical))
    vendored = REPO_ROOT / item.vendored_rel
    vendored.parent.mkdir(parents=True, exist_ok=True)
    vendored.write_text(rendered, encoding="utf-8")
    print(f"re-synced {item.vendored_rel.as_posix()}  (CANONICAL_SHA256 {canonical_sha})")


def main(argv: List[str]) -> int:
    args = [a for a in argv[1:] if a != "--check"]
    check_only = "--check" in argv[1:]
    if len(args) != 1:
        print(
            "usage: python scripts/resync_vendored_libs.py [--check] <path-to-agent-tools-checkout>",
            file=sys.stderr,
        )
        return 2
    agent_tools = Path(args[0]).expanduser().resolve()
    if not agent_tools.is_dir():
        print(f"error: agent-tools checkout not found at {agent_tools}", file=sys.stderr)
        return 2

    # Resolve every canonical path up front; a missing canonical is an INFRA error (the path
    # moved in agent-tools), distinct from body drift.
    pairs = []
    for item in VENDORED:
        canonical = agent_tools / item.canonical_rel
        if not canonical.is_file():
            print(f"error: canonical not found at {canonical}", file=sys.stderr)
            return 2
        pairs.append((item, canonical))

    if check_only:
        worst = 0
        for item, canonical in pairs:
            rc = _check_one(item, canonical)
            # rc 2 (infra) dominates rc 1 (drift) dominates rc 0 — report the most severe.
            worst = max(worst, rc)
        return worst

    for item, canonical in pairs:
        _resync_one(item, canonical)
    return 0


def cli(argv: List[str]) -> int:
    """Run :func:`main`, mapping an UNEXPECTED crash to an INFRA exit code (>=2), never to 1.

    The drift workflow keys on the exit code: rc 1 == real drift ("re-sync the body"), rc >=2 ==
    infra error ("don't touch the body, investigate the check"). A bare Python crash exits 1, which
    would falsely read as drift — so any unexpected exception here is reported as rc 2 instead.
    """
    try:
        return main(argv)
    except Exception:  # noqa: BLE001 - an unexpected crash must be INFRA (>=2), not drift (1)
        import traceback

        traceback.print_exc()
        print("error: drift check crashed (infrastructure error, NOT body drift)", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv))
