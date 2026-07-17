# SYNC-HEADER-BEGIN  (this block is the ONLY delta from the canonical source; the drift
#   guard strips exactly these lines and asserts the remainder is byte-identical to the hash)
# VENDORED COPY of agent-tools/lib/agenttools_config/README.md.
# The canonical source lives in agent-tools (rig provisions it onto a real machine). dev-cli
# was spun out of that umbrella (it lived at lib/agenttools_dev) and VENDORS the shared config
# loader so this repo is self-contained — strategy B, the research-cli/task-cli pattern.
#
# DRIFT GUARD: tests/test_vendored_libs_sync.py reconstructs the canonical content from this file
# (the SYNC-HEADER block removed) and asserts its SHA256 equals CANONICAL_SHA256 below. A local
# edit to this copy, OR a stale copy after the canonical changes upstream, fails CI instead of
# silently diverging.
#
# CANONICAL_SHA256: c0853e1ea51a2fd6dcb87581631e19851aa50ebab6555f5ade00a23a4ae46f27
# CANONICAL_AGENT_TOOLS_COMMIT: c893c1aabb1c1652752f1fc0589e595f5f5d03be
#
# TO RE-SYNC after the canonical changes: run
#   python scripts/resync_vendored_libs.py <path-to-agent-tools>
# (re-copies the canonical body and refreshes CANONICAL_SHA256 + the commit above).
# SYNC-HEADER-END
# agenttools-config — two-layer config cascade loader

A generalized, **tool-agnostic** config-cascade loader for the agent-tools ecosystem,
extracted from `rig-cli`'s `riglib.config`. It owns the LOADING / CASCADE / PATH logic
— **not** any tool's domain schema. Any Python CLI (`rig`, `review`, `task`, …) gets the
same `~/.config/<tool>` + per-repo-overlay semantics from one importable library.

## The cascade (two layers, by location)

| Order | Layer | Path | Role |
| ----- | ----- | ---- | ---- |
| 1 (base) | **Global** | `$XDG_CONFIG_HOME/<tool>/config.yaml` → falls back to `~/.config/<tool>/config.yaml` | machine-wide defaults a developer carries across repos |
| 2 (wins) | **Per-repo** | `<repo_root>/<tool>.yaml` | committed, reproducible source of truth; **overrides** global |

The two layers cascade by **location** — there is no scope flag. A `--config PATH` flag
(passed as `explicit_config=`) replaces the per-repo layer with that exact path.

## Merge rule

A **deep dict merge**, per-repo winning:

- nested **dicts** merge recursively (global `a.b.c` survives a repo file that only sets
  `a.b.d`);
- **lists and scalars replace wholesale** — a list in the repo file fully replaces the
  global list. Lists are atomic decisions, never appended. This keeps the result
  predictable (an inherited global list can't silently grow).

```python
deep_merge({"a": {"x": 1, "y": 2}, "list": [1, 2]},
           {"a": {"y": 9, "z": 3}, "list": [9]})
# -> {"a": {"x": 1, "y": 9, "z": 3}, "list": [9]}
```

## Usage

```python
from pathlib import Path
from agenttools_config import load_config

loaded = load_config(tool="rig", repo_root=Path("/path/to/repo"))

loaded.data          # the cascaded dict (defaults to {} when neither layer exists)
loaded.layers        # ["global:/home/u/.config/rig/config.yaml", "repo:/repo/rig.yaml"]
loaded.global_path   # absolute Path of the global layer, or None if absent
loaded.repo_path     # absolute Path of the per-repo layer, or None if absent
loaded.is_empty      # True when neither layer was present on disk
```

### Plug in your own schema (fail-closed)

This library validates the *shape of the cascade* (a layer must be a YAML mapping, the
file must be readable, the YAML must parse). It validates **none of your domain keys** —
pass a `schema_validate` callable for that. It runs on the merged dict; if it raises, the
exception propagates to the caller unchanged.

```python
def validate(data: dict) -> None:
    if "version" in data and not isinstance(data["version"], int):
        raise ValueError("version must be an int")
    # ... your tool's full schema check here ...

loaded = load_config(tool="rig", repo_root=repo, schema_validate=validate)
# a malformed config raises BEFORE the caller acts on it
```

`rig-cli` can pass its existing `riglib.config.validate` verbatim — the hook signature
(`Callable[[dict], None]`) is exactly that.

### Selecting layers

```python
# Global only (ignore the per-repo file even if it exists):
load_config(tool="rig", repo_root=repo, include_repo=False)

# Skip the machine-wide layer entirely (e.g. a hermetic CI run = repo file only):
load_config(tool="rig", repo_root=repo, include_global=False)

# Replace the per-repo layer with an explicit file (a --config flag). This overrides
# include_repo=False — an explicitly named file is always loaded:
load_config(tool="rig", repo_root=repo, explicit_config=Path("/tmp/override.yaml"))
```

`include_global` and `include_repo` independently gate the two layers; `explicit_config`
substitutes a named file for the per-repo layer (and wins over `include_repo=False`).

## Failure posture

- **Missing layer file → skipped, not an error.** A tool with no config at all yields
  `LoadedConfig(data={}, ...)`. (`explicit_config` is the one exception: a path you named
  explicitly must exist, else `ConfigError`.)
- **Malformed file → `ConfigError`.** An unreadable file, invalid YAML, or a non-mapping
  root each raise `ConfigError` (a `ValueError` subclass) — never a raw PyYAML traceback.
- **`schema_validate` raises → surfaced unchanged.** The hook is sovereign; a fail-closed
  validate must be loud.

## Why stdlib-only at import time

`yaml` is imported **lazily**, inside the loader — importing `agenttools_config` does
**not** require PyYAML. This mirrors the ecosystem's lazy-heavy-imports rule so a CLI's
`--help` / `doctor` / version path stays usable even without PyYAML installed. PyYAML is
only needed when you actually load a config file.

## Public API

| Symbol | Purpose |
| ------ | ------- |
| `load_config(*, tool, repo_root, schema_validate=None, explicit_config=None, include_global=True) -> LoadedConfig` | cascade-load + optionally validate |
| `LoadedConfig` | dataclass: `data`, `tool`, `repo_root`, `global_path`, `repo_path`, `layers`, `is_empty` |
| `global_config_path(tool) -> Path` | resolve the machine-wide layer path (XDG-aware) |
| `repo_config_path(tool, repo_root) -> Path` | resolve the per-repo layer path |
| `deep_merge(base, over) -> dict` | the merge primitive (exposed for reuse/testing) |
| `ConfigError` | raised on a malformed/invalid config (`ValueError` subclass) |

## Installing / importing as a consumer

The package lives under `lib/` in the umbrella repo and builds as the `agenttools-config`
distribution.

```toml
# pyproject.toml of the consumer
[project]
dependencies = ["agenttools-config"]
```

For local/dev installs from the umbrella checkout, point at THIS package's directory
(`lib/agenttools_config/`, where its `pyproject.toml` lives — `lib/pyproject.toml` is the
separate `agenttools-log` distribution and does not build this package):

```sh
pip install -e /path/to/agent-tools/lib/agenttools_config      # editable install
# or, ad-hoc, with uv:
uv run --with /path/to/agent-tools/lib/agenttools_config \
  python -c "from agenttools_config import load_config"
```

### Migration seam for `rig-cli`

`rig-cli`'s `riglib.config` already implements this exact cascade by hand (global +
per-repo, XDG-aware, deep merge with lists replacing). It can become a thin wrapper:
call `agenttools_config.load_config(tool="rig", repo_root=..., schema_validate=validate)`
and keep its rig-specific `validate` / `LoadedConfig` accessors (`agent_tools_source`,
`category(...)`, `defaults`) layered on top. **Wiring that is a deliberate follow-up —
this package ships standalone.**
