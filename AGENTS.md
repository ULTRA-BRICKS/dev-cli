# AGENTS.md — dev-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`dev` is the project-scoped dev/e2e process runner: a standalone Python CLI that runs a repo's
`rig.yaml` scripts and manages background dev servers / e2e jobs, per project, with
safe-by-default process control. It is a peer to `pm-cli` / `research-cli` / `tg-cli`, spun out of
the agent-tools umbrella (it lived at `lib/agenttools_dev`).

Verbs: `dev start|list|status|logs|run <script>|has-script|stop|e2e|env|kill-orphan|clean-logs`
(+ `install-skill`, `--version`, the hidden `--agenttools-dev-probe`).

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
- **`dev kill-orphan` / `dev clean-logs` never accept a free-text pattern; `rules.yaml` edits
  require review quorum to COMMIT.** These commands exist so agents stop generating ad hoc
  `pkill -f <pattern>` / `rm -f <path>` one-liners that need interactive Bash-permission
  approval for every call — the two commands are safe by construction instead: `dev kill-orphan
  --harness <name> --worker <w> [--instance <i>]` and `dev clean-logs <path> [<path> ...]` only
  accept structured fields, which `dev_cli/cleanup_rules.py` validates against `rules.yaml`
  before anything is killed or deleted (an unscoped/under-specified target is refused, not
  best-effort-warned). `rules.yaml` — and `dev_cli/cleanup_rules.py`'s validation/selection
  functions, an EQUAL safety boundary — cannot be COMMITTED with a change unless the task
  code has a passed review-cli quorum: `scripts/git-hooks/check_rules_quorum.py` (wired via a
  worktree-scoped `core.hooksPath`, set by `install.sh`) re-runs `review task <CODE> --check`
  live, for both `pre-commit` and `pre-merge-commit`. See `rules.yaml`'s own header comment
  for the full mechanism, and `dev_cli/cleanup_rules.py`'s module docstring for why the
  match-selection logic (not just the regex-building) lives in the gated file too.
  **Known limits — this is a commit-time speed bump, not a runtime sandbox:** (1) it gates
  `git commit`/normal `git merge`, not a live edit to the WORKING TREE — an agent that edits
  `rules.yaml` or `cleanup_rules.py` with a file tool and runs `dev` without committing sees
  the edited version immediately, no hook involved; (2) a FAST-FORWARD merge (git's default
  when possible) creates no commit and runs neither hook; (3) an already-passed task code on
  a fresh, code-less branch (e.g. `git checkout -b tweak-1 <old-branch>`) satisfies the check
  without a new review of the new diff, because `review task <CODE> --check` is bound to the
  task code's history, not to the specific staged content. Treat it as raising the bar for an
  accidental or drive-by loosening, not as an unforgeable guarantee against a determined
  attacker who already has commit access to this repo.

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
- `ruff check .` — lint (skips `vendor/`). Match CI's pinned version — `pip install
  ruff==0.15.18` — before trusting a local pass/fail; ruff is unpinned upstream by default
  and a newer release can enable new default rules a local `pip install ruff` picks up
  before CI's pin does, giving a false red/green mismatch (see `.github/workflows/tests.yml`).
