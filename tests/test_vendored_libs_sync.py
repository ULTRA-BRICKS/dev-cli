"""Drift guard for the vendored agent-tools library (strategy B).

WHAT this proves: every file under ``vendor/`` is a VENDORED copy of an agent-tools canonical
file (``lib/agenttools_config/*``). Each must stay byte-identical to its canonical body so this
self-contained repo reads the ``rig.yaml`` cascade with the SAME loader the umbrella ships.
Nothing mechanical enforced that before — the SYNC header merely ASKED for it, so a copy could
silently drift while the suite stayed green against a stale vendored lib.

HOW the guard works WITHOUT a network / the agent-tools repo at test time: each vendored file
carries a delimited ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block — the ONLY delta from the
canonical source — plus a pinned ``CANONICAL_SHA256``. This test reconstructs the canonical
content (the file with the SYNC-HEADER block removed) and asserts its SHA256 equals the pinned
hash. So:

- a LOCAL edit to a vendored body changes its SHA → mismatch → CI fails;
- a STALE copy after a canonical changes upstream → the pinned SHA is bumped during the documented
  re-sync (``scripts/resync_vendored_libs.py``), and forgetting to re-copy a body fails this test.

A separate SCHEDULED workflow (``.github/workflows/vendored-libs-drift.yml``) runs the same script
in ``--check`` mode against the LIVE agent-tools canonical to catch upstream drift this pinned-SHA
test can't see.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR = REPO_ROOT / "vendor"

SYNC_BEGIN = "# SYNC-HEADER-BEGIN"
SYNC_END = "# SYNC-HEADER-END"

# Every vendored file the drift guard covers (relative to the repo root).
VENDORED_FILES = [
    "vendor/agenttools_config/__init__.py",
    "vendor/agenttools_config/core.py",
    "vendor/agenttools_config/README.md",
]


def _strip_sync_header(text: str) -> str:
    """Return ``text`` with exactly the ``# SYNC-HEADER-BEGIN … # SYNC-HEADER-END`` block removed.

    The block is the ONLY delta between the vendored copy and the canonical source, so removing
    it reconstructs the canonical content byte-for-byte.
    """
    out: list[str] = []
    skipping = False
    for line in text.split("\n"):
        if line.startswith(SYNC_BEGIN):
            skipping = True
            continue
        if line.startswith(SYNC_END):
            skipping = False
            continue
        if not skipping:
            out.append(line)
    return "\n".join(out)


def _pinned_canonical_sha(text: str) -> str:
    m = re.search(r"# CANONICAL_SHA256:\s*([0-9a-f]{64})", text)
    assert m, "a vendored copy must record a CANONICAL_SHA256 in its SYNC-HEADER block"
    return m.group(1)


@pytest.mark.parametrize("rel", VENDORED_FILES)
def test_vendored_file_exists_with_sync_header(rel):
    path = REPO_ROOT / rel
    assert path.is_file(), f"vendored file missing: {path}"
    text = path.read_text(encoding="utf-8")
    assert SYNC_BEGIN in text and SYNC_END in text, f"{rel}: SYNC-HEADER delimiters must be present"
    assert text.count(SYNC_BEGIN) == 1, f"{rel}: exactly one SYNC-HEADER block"
    assert text.count(SYNC_END) == 1, f"{rel}: exactly one SYNC-HEADER block"
    assert text.index(SYNC_BEGIN) < text.index(SYNC_END), f"{rel}: begin must precede end"


@pytest.mark.parametrize("rel", VENDORED_FILES)
def test_vendored_body_matches_pinned_canonical_sha(rel):
    # THE drift guard: the canonical content reconstructed from each vendored copy must hash to its
    # pinned CANONICAL_SHA256. A drifted/edited vendored body fails here.
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    reconstructed = _strip_sync_header(text)
    got = hashlib.sha256(reconstructed.encode("utf-8")).hexdigest()
    expected = _pinned_canonical_sha(text)
    assert got == expected, (
        f"the vendored {rel} has DRIFTED from the recorded canonical source.\n"
        f"  reconstructed body sha256 = {got}\n"
        f"  pinned CANONICAL_SHA256   = {expected}\n"
        "Re-sync it: `python scripts/resync_vendored_libs.py <path-to-agent-tools>` (then commit), "
        "or — if you intentionally edited the vendored body — update the pinned hash via the same "
        "script so the documented delta is recorded."
    )


def _load_resync_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "resync_vendored_libs", REPO_ROOT / "scripts" / "resync_vendored_libs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_agent_tools(tmp_path: Path, mod, bodies: dict | None = None) -> str:
    """Build a fake agent-tools checkout whose canonical files equal what each vendored copy
    reconstructs (so ``--check`` reports IN SYNC), overriding individual bodies via ``bodies``.
    """
    bodies = bodies or {}
    for item in mod.VENDORED:
        canon = tmp_path / item.canonical_rel
        canon.parent.mkdir(parents=True, exist_ok=True)
        rel = item.vendored_rel.as_posix()
        body = bodies.get(
            rel, _strip_sync_header((REPO_ROOT / item.vendored_rel).read_text(encoding="utf-8"))
        )
        canon.write_text(body, encoding="utf-8")
    return str(tmp_path)


def test_resync_check_exit_codes_contract(tmp_path):
    # the scheduled workflow keys "drift vs infra error" on the script's exit codes — pin all three.
    mod = _load_resync_module()

    # rc 2 — usage error (no path) and a missing canonical path
    assert mod.main(["resync", "--check"]) == 2
    assert mod.main(["resync", "--check", str(tmp_path / "nope")]) == 2

    # rc 0 — every canonical body equals what the vendored copy reconstructs → in sync
    synced = _fake_agent_tools(tmp_path / "ok", mod)
    assert mod.main(["resync", "--check", synced]) == 0

    # rc 1 — one canonical body differs from the vendored copy → real drift dominates
    drifted = _fake_agent_tools(
        tmp_path / "drift",
        mod,
        bodies={"vendor/agenttools_config/core.py": "# a different body\n"},
    )
    assert mod.main(["resync", "--check", drifted]) == 1


def test_resync_cli_maps_a_crash_to_infra_exit_not_drift(tmp_path, monkeypatch):
    # a CRASH (unexpected exception) must exit >=2 (infra error), NOT 1 (which the workflow reads as
    # real drift → "re-sync the body", the wrong advice). cli() wraps main() to guarantee this.
    mod = _load_resync_module()
    synced = _fake_agent_tools(tmp_path, mod)

    def boom(*a, **k):
        raise RuntimeError("simulated infra crash")

    monkeypatch.setattr(mod, "reconstruct_canonical_from_vendored", boom)
    rc = mod.cli(["resync", "--check", synced])
    assert rc >= 2, "an unexpected crash must be an infra error (>=2), never drift (1)"
