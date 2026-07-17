# SYNC-HEADER-BEGIN  (this block is the ONLY delta from the canonical source; the drift
#   guard strips exactly these lines and asserts the remainder is byte-identical to the hash)
# VENDORED COPY of agent-tools/lib/agenttools_config/__init__.py.
# The canonical source lives in agent-tools (rig provisions it onto a real machine). dev-cli
# was spun out of that umbrella (it lived at lib/agenttools_dev) and VENDORS the shared config
# loader so this repo is self-contained — strategy B, the research-cli/task-cli pattern.
#
# DRIFT GUARD: tests/test_vendored_libs_sync.py reconstructs the canonical content from this file
# (the SYNC-HEADER block removed) and asserts its SHA256 equals CANONICAL_SHA256 below. A local
# edit to this copy, OR a stale copy after the canonical changes upstream, fails CI instead of
# silently diverging.
#
# CANONICAL_SHA256: 1a17246d530a0bdec29d15f7c2431ca68d109ba4c795365e3bdcc1f231e6b5d2
# CANONICAL_AGENT_TOOLS_COMMIT: c893c1aabb1c1652752f1fc0589e595f5f5d03be
#
# TO RE-SYNC after the canonical changes: run
#   python scripts/resync_vendored_libs.py <path-to-agent-tools>
# (re-copies the canonical body and refreshes CANONICAL_SHA256 + the commit above).
# SYNC-HEADER-END
"""agenttools_config — a generalized two-layer config-cascade loader.

WHAT THIS IS
    The tool-agnostic LOADING / CASCADE / PATH logic lifted out of ``rig-cli``'s
    ``riglib.config`` so any ecosystem CLI can do::

        from agenttools_config import load_config

        loaded = load_config(tool="rig", repo_root=Path("/path/to/repo"))
        cfg = loaded.data          # the cascaded dict
        loaded.layers              # which layers were present, in merge order

    Two layers, cascaded by **location** (no scope flag):

    1. **Global** — ``$XDG_CONFIG_HOME/<tool>/config.yaml`` (or, when ``XDG_CONFIG_HOME``
       is unset, ``~/.config/<tool>/config.yaml``). Machine-wide defaults a developer
       carries across repos.
    2. **Per-repo** — ``<tool>.yaml`` at the repo root. Committed by default; the
       reproducible source of truth, and it **overrides** the global layer.

    The merge is a deep dict merge: per-repo keys win, nested dicts merge recursively,
    scalars and lists replace wholesale (a list in the repo file fully replaces the
    global list — lists are treated as atomic decisions, not appended, to keep the
    result predictable).

    What is DELIBERATELY left out: any tool's domain schema. The reference impl knew
    rig's keys (``skills``/``ci``/``mcp``/…); this library knows none. Pass a
    ``schema_validate`` callable to enforce your own schema fail-closed.

WHY A SHARED LIB
    ``rig-cli`` already ships this cascade by hand. ``review-cli``, ``task-cli``, and any
    future Python CLI want the SAME ``~/.config/<tool>`` + per-repo overlay semantics.
    One importable loader keeps the path resolution, the XDG handling, and the merge
    rule identical everywhere instead of re-derived per tool.

INVARIANTS
    - **stdlib only at import time.** ``yaml`` is imported lazily inside the loader so
      ``<tool> --help`` / ``doctor`` work even when PyYAML is not installed.
    - **Fail-closed on a malformed file** — a non-mapping root, unreadable file, or
      invalid YAML raises :class:`ConfigError`, never a raw ``yaml`` traceback.
    - **The ``schema_validate`` hook is sovereign.** It runs on the merged dict after the
      cascade; if it raises, the exception is surfaced to the caller unchanged.
"""

from __future__ import annotations

from .core import (
    ConfigError,
    LoadedConfig,
    deep_merge,
    global_config_path,
    load_config,
    repo_config_path,
)

__all__ = [
    "ConfigError",
    "LoadedConfig",
    "deep_merge",
    "global_config_path",
    "load_config",
    "repo_config_path",
]

__version__ = "0.1.0"
