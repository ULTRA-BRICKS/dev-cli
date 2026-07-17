# SYNC-HEADER-BEGIN  (this block is the ONLY delta from the canonical source; the drift
#   guard strips exactly these lines and asserts the remainder is byte-identical to the hash)
# VENDORED COPY of agent-tools/lib/agenttools_config/core.py.
# The canonical source lives in agent-tools (rig provisions it onto a real machine). dev-cli
# was spun out of that umbrella (it lived at lib/agenttools_dev) and VENDORS the shared config
# loader so this repo is self-contained — strategy B, the research-cli/task-cli pattern.
#
# DRIFT GUARD: tests/test_vendored_libs_sync.py reconstructs the canonical content from this file
# (the SYNC-HEADER block removed) and asserts its SHA256 equals CANONICAL_SHA256 below. A local
# edit to this copy, OR a stale copy after the canonical changes upstream, fails CI instead of
# silently diverging.
#
# CANONICAL_SHA256: 10ab4bbe90d7c328a2347d388364ac8de7e4e6ea4514fc78072bc579d9cbf664
# CANONICAL_AGENT_TOOLS_COMMIT: c893c1aabb1c1652752f1fc0589e595f5f5d03be
#
# TO RE-SYNC after the canonical changes: run
#   python scripts/resync_vendored_libs.py <path-to-agent-tools>
# (re-copies the canonical body and refreshes CANONICAL_SHA256 + the commit above).
# SYNC-HEADER-END
"""Two-layer config cascade loader — path resolution + deep merge + pluggable validate.

WHAT THIS FILE IS
    The engine behind ``agenttools_config.load_config``. It resolves the two layer paths
    (global ``~/.config/<tool>/config.yaml`` honoring ``$XDG_CONFIG_HOME``; per-repo
    ``<tool>.yaml`` at the repo root), parses each present layer, deep-merges them with
    the per-repo layer winning, then runs an optional caller-supplied ``schema_validate``
    hook on the result.

HOW IT'S REACHED AT RUNTIME
    A CLI calls ``load_config(tool="...", repo_root=...)`` early in a command (after it
    has located the repo root). The returned :class:`LoadedConfig` carries the merged
    ``data`` plus provenance (which absolute paths each layer came from, and the ordered
    ``layers`` list) so the CLI can show "loaded global + repo" in ``status``/``doctor``.

INVARIANTS / PAST BUGS
    - ``yaml`` is imported LAZILY inside ``_load_yaml`` — importing this module must not
      require PyYAML (the ecosystem's lazy-heavy-imports rule; ``--help`` stays usable
      offline / dependency-free).
    - The merge replaces lists wholesale (atomic), and merges nested dicts recursively.
      This matches ``rig-cli``'s reference semantics; appending lists was rejected there
      as unpredictable. Do NOT "improve" it to concatenate without changing the docs and
      every consumer's expectations.
    - Path resolution reads ``XDG_CONFIG_HOME`` from the environment on EVERY call (not
      cached at import) so tests and per-process overrides take effect.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# A validate hook: given the merged config dict, raise on any violation (fail-closed).
# Return value is ignored. Kept as a plain Callable so a consumer can pass its existing
# ``validate(data)`` function verbatim (e.g. rig's ``riglib.config.validate``).
SchemaValidate = Callable[[dict], None]


class ConfigError(ValueError):
    """Raised on a malformed/invalid config (fail-closed before a caller acts on it).

    Subclasses ``ValueError`` so a consumer that wraps a broad ``except ValueError`` still
    catches it, while a consumer that wants the specific type can catch ``ConfigError``.
    """


def global_config_path(tool: str) -> Path:
    """Path to the machine-wide layer: ``$XDG_CONFIG_HOME/<tool>/config.yaml``.

    Falls back to ``~/.config/<tool>/config.yaml`` when ``XDG_CONFIG_HOME`` is unset or
    empty. The env var is read on every call so an override (or a test monkeypatch) is
    honored immediately.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / tool / "config.yaml"


def repo_config_path(tool: str, repo_root: Path) -> Path:
    """Path to the per-repo layer: ``<repo_root>/<tool>.yaml``."""
    return Path(repo_root) / f"{tool}.yaml"


def deep_merge(base: dict, over: dict) -> dict:
    """Recursive dict merge; ``over`` wins. Nested dicts merge, lists/scalars replace.

    Returns a NEW top-level dict (the merge does not mutate ``base`` or ``over``); each
    level of merged nested dicts is likewise freshly allocated. A list or scalar in
    ``over`` replaces the corresponding value in ``base`` wholesale — lists are atomic,
    never concatenated.

    Note on aliasing: leaf containers that are NOT themselves merged (a list/scalar, or a
    sub-dict present in only one input) are stored by reference, not deep-copied. So
    `deep_merge` is safe to *call* repeatedly, but mutating a list/leaf *inside the result*
    can reach back into an input. ``load_config`` never does this — each layer's parsed
    YAML is local and discarded — but a direct caller wanting full isolation should
    ``copy.deepcopy`` the result.
    """
    out = dict(base)
    for key, value in over.items():
        existing = out.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            out[key] = deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _load_yaml(path: Path) -> dict:
    """Parse a YAML file to a dict. Lazy ``yaml`` import; an empty file → ``{}``.

    Fail-closed: an unreadable file, invalid YAML, or a non-mapping root each raise
    :class:`ConfigError` (never a raw PyYAML traceback). The lazy import keeps this
    module stdlib-only at import time.
    """
    import yaml  # lazy: keeps `<tool> --help` dependency-free (no PyYAML required to import)

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"config {path} must be a YAML mapping, got {type(data).__name__}"
        )
    return data


@dataclass
class LoadedConfig:
    """A cascaded config plus provenance of where each layer came from.

    ``data`` is the merged dict. ``global_path`` / ``repo_path`` are the absolute paths of
    the layers that were actually present on disk (``None`` if that layer was absent).
    ``layers`` is the ordered, human-readable list of layers that contributed, in merge
    order (e.g. ``["global:/home/u/.config/rig/config.yaml", "repo:/repo/rig.yaml"]``) —
    handy for a ``status``/``doctor`` line.
    """

    data: dict
    tool: str
    repo_root: Path
    global_path: Optional[Path] = None
    repo_path: Optional[Path] = None
    layers: list = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """True when neither layer was present on disk (the config is all defaults)."""
        return self.global_path is None and self.repo_path is None


def load_config(
    *,
    tool: str,
    repo_root: Path,
    schema_validate: Optional[SchemaValidate] = None,
    explicit_config: Optional[Path] = None,
    include_global: bool = True,
    include_repo: bool = True,
) -> LoadedConfig:
    """Cascade-load ``<tool>``'s config for ``repo_root``.

    Layers, in merge order (later wins):

    1. **Global** — ``$XDG_CONFIG_HOME/<tool>/config.yaml`` (or ``~/.config/...``). Skipped
       when ``include_global=False``.
    2. **Per-repo** — ``<repo_root>/<tool>.yaml``. Skipped when ``include_repo=False`` (use
       that, with ``include_global=True``, to load the global layer ONLY). If
       ``explicit_config`` is given (e.g. from a ``--config PATH`` flag) it REPLACES the
       per-repo layer with that path, which must exist; ``explicit_config`` overrides
       ``include_repo=False``.

    A missing layer file is simply skipped (not an error) — a tool with no config at all
    yields ``LoadedConfig(data={}, ...)``. After merging, ``schema_validate`` (if given) is
    called on the merged dict; if it raises, that exception propagates to the caller
    unchanged (fail-closed). The hook owns the tool's domain schema; this loader owns none.
    """
    repo_root = Path(repo_root).resolve()
    merged: dict = {}
    layers: list = []
    gpath: Optional[Path] = None
    rpath: Optional[Path] = None

    if include_global:
        # Resolve so LoadedConfig.global_path is absolute even under a relative
        # XDG_CONFIG_HOME — symmetric with repo_root above (else gpath / is_file() would
        # be cwd-dependent and the recorded path non-absolute, contrary to the docs).
        gpath = global_config_path(tool).resolve()
        if gpath.is_file():
            merged = deep_merge(merged, _load_yaml(gpath))
            layers.append(f"global:{gpath}")

    if explicit_config is not None:
        rpath = Path(explicit_config).resolve()
        if not rpath.is_file():
            raise ConfigError(f"--config file not found: {rpath}")
        merged = deep_merge(merged, _load_yaml(rpath))
        layers.append(f"config:{rpath}")
    elif include_repo:
        rpath = repo_config_path(tool, repo_root)
        if rpath.is_file():
            merged = deep_merge(merged, _load_yaml(rpath))
            layers.append(f"repo:{rpath}")

    if schema_validate is not None:
        # Sovereign: the hook owns the domain schema. Let whatever it raises surface to the
        # caller unchanged — do not swallow or re-wrap it (a fail-closed validate must be
        # loud). The loader's own ConfigErrors above already fired before we got here.
        schema_validate(merged)

    return LoadedConfig(
        data=merged,
        tool=tool,
        repo_root=repo_root,
        global_path=gpath if gpath and gpath.is_file() else None,
        repo_path=rpath if rpath and rpath.is_file() else None,
        layers=layers,
    )
