"""Drift guard for the package version literal.

WHAT this proves: `dev_cli.__version__` (what `dev --version`/`dev version` actually print,
per `test_dev_cli.py`'s version tests) and `pyproject.toml`'s `[project] version` (what `pip`/
`gh ship`'s release gate treat as the package's declared version) are two separate literals
with no runtime link between them (`dev_cli/__init__.py` does not read `pyproject.toml`).
Nothing previously caught them drifting apart - a release that bumped one and forgot the
other would ship a build whose `--version` output silently disagreed with its declared
package version (2026-09-09 review finding on the dev-cli#27 stop-race release).

HOW the guard works: mirrors `test_ruff_pin_sync.py`'s approach - regex out the version
literal from each file and assert they agree. A bump that updates `pyproject.toml` but
forgets `dev_cli/__init__.py`, or vice versa, now fails this test instead of silently
shipping a mismatched `--version` string.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_INIT_RE = re.compile(r'__version__\s*=\s*"(\d+\.\d+\.\d+)"')
_PYPROJECT_RE = re.compile(r'^version\s*=\s*"(\d+\.\d+\.\d+)"', re.MULTILINE)


def _extract_one(path: Path, pattern: re.Pattern[str], label: str) -> str:
    text = path.read_text(encoding="utf-8")
    matches = pattern.findall(text)
    assert matches, f"{label}: could not find the version literal in {path}"
    distinct = set(matches)
    assert len(distinct) == 1, (
        f"{label}: found more than one distinct version literal in {path}: {matches}"
    )
    return matches[0]


def test_package_version_matches_pyproject() -> None:
    init_version = _extract_one(
        REPO_ROOT / "dev_cli" / "__init__.py", _INIT_RE, "dev_cli/__init__.py __version__"
    )
    pyproject_version = _extract_one(
        REPO_ROOT / "pyproject.toml", _PYPROJECT_RE, "pyproject.toml [project] version"
    )
    assert init_version == pyproject_version, (
        "dev_cli.__version__ has drifted from pyproject.toml's declared version - a release "
        f"must bump both together (found dev_cli/__init__.py={init_version!r}, "
        f"pyproject.toml={pyproject_version!r})"
    )
