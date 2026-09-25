"""dev_cli - project-scoped development command helpers (the ``dev`` CLI).

Accessed via: the installed ``dev`` console script, or ``python -m dev_cli``.

dev-cli was spun out of the agent-tools umbrella (it lived at ``lib/agenttools_dev``) to
become a first-class peer to the sibling ecosystem CLIs (pm-cli / research-cli / tg-cli).
It VENDORS the shared ``agenttools_config`` loader under ``vendor/`` (strategy B, the proven
research-cli/task-cli pattern) so the repo is self-contained; a pinned-SHA drift guard keeps
the vendored copy single-source with the agent-tools canonical.

Assumptions: importing this package must stay stdlib-only; commands that need rig.yaml or
process inspection load their dependencies and platform helpers inside the command path.
"""

from __future__ import annotations

__version__ = "0.2.0"

__all__ = ["__version__"]
