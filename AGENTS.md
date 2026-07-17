# AGENTS.md — dev-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`dev` is the project-scoped dev/e2e process runner: a standalone Python CLI that runs a repo's
`rig.yaml` scripts and manages background dev servers / e2e jobs, per project, with
safe-by-default process control. It is a peer to `pm-cli` / `research-cli` / `tg-cli`, spun out of
the agent-tools umbrella (it lived at `lib/agenttools_dev`).

Verbs: `dev start|list|status|logs|run <script>|has-script|stop|e2e|env` (+ `install-skill`,
`--version`, the hidden `--agenttools-dev-probe`).

## Hard rules

- **Stdlib-only at import time.** `dev_cli/cli.py` and `dev_cli/install.py` import only the
  standard library at module load. The vendored `agenttools_config` and PyYAML are imported
  LAZILY inside `_load_rig_config`, so `dev --help` / `dev --version` / the probe run with zero
  third-party imports. `tests/test_dev_cli.py::test_package_import_is_stdlib_only` guards this.
- **`dev stop` is safe-by-default and must stay that way.** It sends SIGTERM only after a process
  passes BOTH checks: it looks like a development/e2e tool AND its cwd/command path is scoped to
  the repo (or an explicit `DEV_PROJECT_PATHS` root). Never loosen either check without a test
  proving the new boundary. `dev run` / `dev start` apply the SAME command-shape validation:
  destructive heads, inline redirection, and command substitution are refused.
- **The vendored `agenttools_config` is single-source with agent-tools.** `vendor/` is a
  byte-identical copy of the agent-tools canonical `lib/agenttools_config/`. NEVER hand-edit a
  file under `vendor/` — fixes land upstream in agent-tools, then re-sync here
  (`python scripts/resync_vendored_libs.py <path-to-agent-tools>`, then commit). The drift guard
  (`tests/test_vendored_libs_sync.py`) fails CI on any drift; `ruff` and the formatter skip
  `vendor/` on purpose.
- **Two names are external wire contracts — do NOT rename them.** The hidden
  `--agenttools-dev-probe` flag prints the exact token `agenttools-dev` (agent-tools
  `ci/ship/ship.sh` + the portable git-hooks probe a PATH `dev` with it), and the
  `.agenttools-dev` per-worktree state-dir name is where running-process state lives. Renaming
  either silently breaks ship's `dev`-detection or orphans live state.
- **The `_UV_RUN_*` option tables mirror agent-tools' `orchestrator-stays-thin`.** The uv-run
  flag classification in `cli.py` is intentionally duplicated from that agent-hook (they gate the
  same class of inline-code smuggling). Keep the two in sync; a note in each points at the other.

## Style

- Conventional commits.
- English-only code, comments, and docs.
- No dead code, no underscore-prefixed unused params, no type escape hatches.
- Keep `cli.py`'s argparse thin; behavior lives in the sibling helper functions.
- Add a test with every behavior change. TDD red-first is the house style.

## Tests

- `python -m pytest -q` — the unit suite. Fast, hermetic; every process/signal/`ps`/`lsof` helper
  is monkeypatched, so tests never touch real processes. `conftest.py` puts the repo root +
  `vendor/` on `sys.path` so the suite runs straight from a checkout with no install step.
- `ruff check .` — lint (skips `vendor/`).
