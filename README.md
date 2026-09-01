# dev-cli

`dev-cli` installs the **`dev`** CLI: a small project-scoped front door for the development
actions agents run constantly — running named `rig.yaml` scripts and managing configured dev
servers and e2e jobs — without widening permission to arbitrary shell or process control.

It is a first-class peer to the sibling ecosystem CLIs (`pm-cli` / `research-cli` / `tg-cli`).
It was spun out of the agent-tools umbrella, where it lived at `lib/agenttools_dev`; it VENDORS
the shared `agenttools_config` cascade loader under `vendor/` (strategy B, the proven
research-cli/task-cli pattern) so the repo is self-contained, and a pinned-SHA drift guard keeps
the vendored copy single-source with the agent-tools canonical.

It is stdlib-only at import time. Reading `rig.yaml` uses the vendored `agenttools_config` +
PyYAML lazily inside the command that needs it, so `dev --help`, `dev --version`, and the
`--agenttools-dev-probe` probe do not import PyYAML.

## Install

```bash
# from a local clone
./install.sh

# or piped from curl
curl -fsSL https://raw.githubusercontent.com/alex-mextner/dev-cli/main/install.sh | bash
```

`install.sh` symlinks `bin/dev` into `~/.local/bin`, ensures PyYAML is available (needed to read
`rig.yaml`), registers the `dev` agent skill so harnesses auto-discover it, and removes any prior
`agenttools-dev` uv-tool install it supersedes.

## Commands

```bash
dev run [--repo-only] <script> [-- <args>...]
dev has-script [--repo-only] <script>
dev start <target> [-- <args>...]
dev list
dev status [target]
dev logs <target> [--tail N]
dev e2e run <target> [-- <args>...]
dev e2e status <target>
dev e2e logs <target> [--tail N]
dev e2e stop <target>
dev stop <target>
dev stop --pid <pid>
dev stop --port <port>
dev stop --pgid <pgid>
dev env --add-project <path>
dev kill-orphan --harness <name> --worker <w> [--instance <i>]
dev clean-logs <path> [<path> ...]
dev install-skill
dev --version
```

## Config shape

Keep ordinary named commands in top-level `scripts:`:

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  typecheck: uv run mypy .
  server: pnpm run dev
  e2e: pnpm exec playwright test
  e2e-smoke: pnpm exec playwright test --project=chromium
```

Use the small `dev:` section only when lifecycle metadata matters, such as a server port or an
e2e job that can be started/listed/stopped. The command strings still live in top-level
`scripts:`; `dev:` references them by name and adds metadata.

```yaml
dev:
  server:
    script: server
    url: http://localhost:5173
    ready_url: http://localhost:5173
    ports: [5173]
    process_matchers: ["pnpm run dev", "vite"]
    logs_root: .dev/logs/server
  e2e:
    script: e2e
    requires_server: true
    artifacts_root: test-results
    logs_root: .dev/logs/e2e
    jobs:
      smoke:
        script: e2e-smoke
        requires_server: true
        artifacts_root: test-results/smoke
        logs_root: .dev/logs/e2e-smoke
```

`dev start/list/stop` commands are intentionally limited to development/e2e runners. Obvious
destructive command heads such as `rm`, `kill`, `git reset`, or `git clean` are refused; keep
those outside `dev`. Docker-based e2e runners such as `docker compose up e2e` are allowed.

### `dev run`

`dev run <script>` locates the current repo root, reads the merged `rig.yaml` cascade via
`agenttools_config`, and executes the named top-level `scripts:` entry from the repo root. If no
top-level script exists with that name, it can run an e2e target by resolving `dev.e2e.script` or
`dev.e2e.jobs.<name>.script` back to top-level `scripts:`. Script commands pass through the same
development/e2e safety validation as lifecycle commands, so destructive raw shell stays outside
`dev`. Project-local shell wrappers such as `bash scripts/test.sh` are allowed; shell `-c`
payloads are recursively checked so wrappers like `bash -lc 'npm test'` can run while
`bash -lc 'rm -rf .'` is refused. Inline redirection and command substitution are refused in
`scripts:` commands; put complex logging or shell composition into a project-local wrapper script
and invoke that script.

Script values can be either a string command or a mapping with `cmd:`:

```yaml
scripts:
  test: uv run --with pytest pytest tests/
  web:
    cmd: npm run dev
```

Extra args after `--` are shell-quoted before they are appended:

```bash
dev run test -- -q "tests/unit path"
```

Missing scripts or invalid script config exit `2` with an actionable error.

`dev has-script <script>` is a quiet existence check for portable shell hooks. It uses the same
merged rig.yaml loader as `dev run`, so hooks do not parse YAML with ad hoc shell code.
`dev run --repo-only <script>` and `dev has-script --repo-only <script>` ignore the machine-wide
rig config and read only the repo's committed `rig.yaml`.

### `dev start`, `dev list`, `dev status`, and `dev logs`

`dev start <target>` starts a configured `dev.server` target (named `server`), `dev.e2e` (named
`e2e`), or `dev.e2e.jobs.<target>` in the background from the repo root, records its
pid/process-group under Git's private path for the current worktree, and prints the pid. Extra
args after `--` are shell-quoted and appended.

`dev list` prints configured targets and any recorded running/stale pids. `dev status [target]`
reports configured/running/stale state, and for e2e jobs with `artifacts_root` and `logs_root`
the latest artifact/log path it can find. `dev logs <target>` prints the configured target log,
usually from the latest `logs_root` directory (`--tail N` for a bounded tail). Configured
`logs_root` and `artifacts_root` paths must resolve inside the current repo or
`DEV_PROJECT_PATHS`.

### `dev e2e`

`dev e2e run/status/logs/stop <target>` are first-class aliases for configured `dev.e2e` /
`dev.e2e.jobs.<target>` jobs. They exist so e2e lifecycle commands can be allowlisted as `dev:*`
while raw Docker/Playwright/process probing remains behind the CLI.

### `dev stop --pid/--port/--pgid`

`dev stop` resolves a process by pid or by listening TCP port, then validates both:

- the process command looks like a development tool (`npm`, `pnpm`, `yarn`, `bun`, `vite`, `next`,
  `webpack`, `tsx`, `node`, `python`, `uv`, `pytest`, `cargo`, `go`, `make`, `docker`, and similar
  runners);
- the process cwd points inside the current repo root or an explicitly allowed extra project root.
  If cwd cannot be inspected, `dev` refuses to stop the process rather than trusting arbitrary
  path-looking argv tokens.

Only after both checks pass does it send `SIGTERM`. `--port` uses `lsof` to map a listening port
to a pid; `--pgid` validates visible processes in the group before signalling it.

### `dev kill-orphan` and `dev clean-logs`

Both commands exist so agents stop generating ad hoc `pkill -f <pattern>` / `rm -f <path>`
one-liners for routine harness cleanup — every such shell command needs interactive
Bash-permission approval, and an inconsistent free-text pattern (e.g. a worker-index-only
`pkill` with no unique suffix) can accidentally match an unrelated process. Neither command
accepts a free-text pattern at all: every target is validated against `rules.yaml` (see that
file's own header comment) before anything is killed or deleted, and an under-specified or
unscoped target is refused outright, not best-effort-warned.

```bash
# Kill exactly one E2E harness instance (safest — a specific 8-hex instance id):
dev kill-orphan --harness vscode-e2e-harness --worker 2 --instance 19fd16aa

# Reap a whole worker slot before relaunch (instance omitted — a crashed prior run's
# hex suffix is unknown, but the match still requires the exact --user-data-dir= anchor
# and this one worker index; see rules.yaml for why this is still safe):
dev kill-orphan --harness vscode-e2e-harness --worker 2

# Delete explicit debug/run logs under an allowed temp root:
dev clean-logs /tmp/hyp1404-run.log /tmp/hyp-inspector-focus/run-reload.log
```

`rules.yaml` (and `dev_cli/cleanup_rules.py`'s validation functions, an equal safety boundary)
cannot be edited without a passed review-cli quorum for the task code — see AGENTS.md
"`dev kill-orphan` / `dev clean-logs` never accept a free-text pattern" for the full mechanism.

### Multi-project sessions

`DEV_PROJECT_PATHS` is an `os.pathsep`-separated list of extra project roots allowed for the
current session. Agents should set it only when the user explicitly asks to work across multiple
projects. `dev env --add-project <path>` prints a shell export line instead of mutating the
parent environment:

```bash
eval "$(dev env --add-project ../api)"
```

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | success |
| script exit | `dev run` returns the script command's exit code |
| `2` | invalid args/config, missing script, unsafe stop target |
| `127` | required platform helper/dependency missing |

## The vendored `agenttools_config`

`vendor/agenttools_config/` is a byte-identical vendored copy of the agent-tools canonical
`lib/agenttools_config/`, carrying a `# SYNC-HEADER-BEGIN … # SYNC-HEADER-END` block with a pinned
`CANONICAL_SHA256`. The drift guard (`tests/test_vendored_libs_sync.py`) reconstructs the
canonical body (block stripped) and asserts it hashes to the pin, so a local edit or a stale copy
fails CI. A scheduled workflow (`.github/workflows/vendored-libs-drift.yml`) runs the re-sync
script in `--check` mode against the live agent-tools canonical to catch upstream drift the pinned
test can't see. To re-sync after an intentional canonical change:

```bash
python scripts/resync_vendored_libs.py /path/to/agent-tools   # then commit
```

## Backward-compat wire contracts

`dev-cli` preserves two names as external contracts (agent-tools `ci/ship/ship.sh` and the
portable git-hooks depend on them): the hidden `--agenttools-dev-probe` flag (prints the exact
token `agenttools-dev`) and the `.agenttools-dev` per-worktree state-dir name. Do not rename them.

## Tests

```bash
python -m pytest -q      # hermetic; monkeypatches all process/signal helpers, no real processes
ruff check .              # match CI's pinned version: pip install ruff==0.15.18 (see .github/workflows/tests.yml)
```

<!-- rig:ecosystem-block -->
## Ecosystem

Part of the [HyperIDE.ai](https://hyperide.ai) agent toolchain:

- **[tg-cli](https://github.com/alex-mextner/tg-cli)** — simple Telegram CLI to send messages, photos & files, and a two-way agent bridge (reports, Q→buttons, voice/rich)
- **[review-cli](https://github.com/alex-mextner/review-cli)** — multi-model read-only code review from one command: diff review, cited quorum, brainstorm, visual review, and interactive spec-review tooling. Read-only, CLI-first, harness-agnostic.
- **[agent-tools](https://github.com/alex-mextner/agent-tools)** — the shared catalog `rig` applies: portable agent skills, agent-hooks, the global git-hook dispatcher, CI gates, and MCP servers
- **[draw-cli](https://github.com/alex-mextner/draw-cli)** — text-to-image via Hugging Face
- **[3d-cli](https://github.com/alex-mextner/3d-cli)** — scriptable CLI for the full 3D FDM lifecycle: modeling, mesh repair, slicing, and print monitoring
- **[task-cli](https://github.com/alex-mextner/task-cli)** — enforced ticket-system CLI for agents (GitHub Issues / Linear): acceptance criteria, motivation, and user-impact gates before work starts
- **[research-cli](https://github.com/alex-mextner/research-cli)** — multi-provider research / panel CLI: puts a question to a panel of models, each through a research lens, then synthesizes one attributed, fact-checked note
- **[pm-cli](https://github.com/alex-mextner/pm-cli)** — autonomous project-manager coordinator over the task/tg/rig ecosystem: keeps a work queue as a deterministic projection of an append-only event log, reconciling on unforgeable evidence rather than dispatching or editing code itself
- **[rig-cli](https://github.com/alex-mextner/rig-cli)** — sets up a repo (and a dev machine) from a committed `rig.yaml`: skills, agent-hooks, git-hook dispatcher, CI gates, MCP, and the personal CLI ecosystem itself
- **[hyperide.ai](https://hyperide.ai)** — Figma replacement inside VS Code. Edit React components directly through AST/LSP without AI hallucinations, token waste, or context-window limits. Works for indie vibe-coding and for enterprise teams with split design/dev roles.
<!-- /rig:ecosystem-block -->
