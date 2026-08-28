"""Drift guard for the pinned ruff version literal.

WHAT this proves: `.github/workflows/tests.yml` installs a pinned ruff version (not a bare
`pip install ruff` — see that file's own comment for why: an unpinned linter turns CI
non-deterministic, since a new ruff release can enable new default rules and flip a
previously-green PR red with no code change). `pyproject.toml`'s `[tool.ruff]
required-version` is the single MACHINE-enforced source of truth (a mismatched local ruff
binary hard-fails instead of silently reporting different findings than CI) — but the same
literal is also repeated, for a human reading CI logs or docs without pyproject.toml open, in
the workflow's own `pip install` line and in README.md / AGENTS.md. Those doc copies have no
ruff-level enforcement, so nothing previously caught them drifting apart (2026-08-28 review
finding).

HOW the guard works: regex out every `pip install ruff==X`/`required-version` occurrence per
file (not just the first — a stale second literal elsewhere in the same file must fail this
test too) and assert each file has exactly one distinct version, and that all four files agree
with each other. A bump that updates pyproject.toml but forgets README.md, AGENTS.md, or the
workflow's `pip install` line now fails this test instead of silently reintroducing the exact
CI/local mismatch the pin exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_PYPROJECT_RE = re.compile(r'required-version\s*=\s*"==(\d+\.\d+\.\d+)"')
_INSTALL_PIN_RE = re.compile(r"pip install\s+ruff==(\d+\.\d+\.\d+)")


def _extract_one(path: Path, pattern: re.Pattern[str], label: str) -> str:
    """Return the single distinct version `pattern` finds in `path`.

    Uses findall (not search) so a stale SECOND literal elsewhere in the same file — not just
    the first match — fails the guard instead of silently surviving behind an earlier, correct
    one.
    """
    text = path.read_text(encoding="utf-8")
    matches = pattern.findall(text)
    assert matches, f"{label}: could not find the pinned ruff version in {path}"
    distinct = set(matches)
    assert len(distinct) == 1, (
        f"{label}: found more than one distinct ruff version literal in {path}: {matches}"
    )
    return matches[0]


def test_ruff_pin_matches_across_pyproject_workflow_and_docs() -> None:
    pyproject_version = _extract_one(
        REPO_ROOT / "pyproject.toml", _PYPROJECT_RE, "pyproject.toml [tool.ruff] required-version"
    )
    workflow_version = _extract_one(
        REPO_ROOT / ".github" / "workflows" / "tests.yml",
        _INSTALL_PIN_RE,
        "tests.yml lint job's pip install",
    )
    readme_version = _extract_one(
        REPO_ROOT / "README.md", _INSTALL_PIN_RE, "README.md Tests section"
    )
    agents_version = _extract_one(
        REPO_ROOT / "AGENTS.md", _INSTALL_PIN_RE, "AGENTS.md Tests section"
    )

    versions = {
        "pyproject.toml": pyproject_version,
        "tests.yml": workflow_version,
        "README.md": readme_version,
        "AGENTS.md": agents_version,
    }
    distinct = set(versions.values())
    assert len(distinct) == 1, (
        "the pinned ruff version has drifted apart across files — pyproject.toml's "
        "required-version is the enforced source of truth, but a bump must update ALL of "
        f"these together (found: {versions})"
    )
