#!/usr/bin/env bash
# install.sh — install the `dev` CLI (Python 3).
# Works from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://git.hyperide.ai/ultrabricks/dev-cli/raw/branch/main/install.sh | bash
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="dev"
REPO="dev-cli"
FORGEJO_HOST="git.hyperide.ai"
FORGEJO_OWNER="ultrabricks"
ENTRY="bin/dev"   # path inside repo root
CLONE_BASE="${XDG_DATA_HOME:-$HOME/.local/share}"

# ── locate source dir ─────────────────────────────────────────────────────────
_script_dir=""
if [[ -n "${BASH_SOURCE[0]:-}" && "${BASH_SOURCE[0]}" != "bash" ]]; then
  _script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$_script_dir" && -f "$_script_dir/$ENTRY" ]]; then
  SRC="$_script_dir"
  echo "dev: using local clone at $SRC"
else
  mkdir -p "$CLONE_BASE"
  CLONE_DIR="$CLONE_BASE/$REPO"
  EXPECT_URL="https://$FORGEJO_HOST/$FORGEJO_OWNER/$REPO.git"
  if [[ -d "$CLONE_DIR/.git" ]]; then
    actual_url="$(git -C "$CLONE_DIR" remote get-url origin 2>/dev/null || echo "")"
    if [[ "$actual_url" != "$EXPECT_URL" ]]; then
      echo "ERROR: $CLONE_DIR exists but its origin is '$actual_url', not $EXPECT_URL." >&2
      echo "       Remove that directory or fix its remote, then re-run." >&2
      exit 1
    fi
    echo "dev: updating existing clone at $CLONE_DIR"
    git -C "$CLONE_DIR" pull --ff-only
  else
    echo "dev: cloning $EXPECT_URL into $CLONE_DIR"
    git clone "$EXPECT_URL" "$CLONE_DIR"
  fi
  SRC="$CLONE_DIR"
fi

# ── bin dir ───────────────────────────────────────────────────────────────────
BIN="$HOME/.local/bin"
mkdir -p "$BIN"

if [[ ":$PATH:" != *":$BIN:"* ]]; then
  echo ""
  echo "  NOTE: $BIN is not on your PATH."
  echo "  Add this to your ~/.bashrc or ~/.zshrc and restart your shell:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
fi

# ── dependency: pyyaml (rig.yaml parse) ────────────────────────────────────────
# `dev --help` / `dev --version` / the probe are stdlib-only, but any command that reads
# rig.yaml (dev run / start / list / status / …) needs PyYAML via the vendored config loader.
if ! python3 -c 'import yaml' 2>/dev/null; then
  echo "dev: pyyaml not found, attempting: python3 -m pip install --user pyyaml"
  if ! python3 -m pip install --user pyyaml 2>/dev/null; then
    echo ""
    echo "  WARNING: could not install pyyaml. Reading rig.yaml needs it; commands that read"
    echo "  rig.yaml print an actionable error until it is present. Install: pip install --user pyyaml"
    echo ""
  fi
fi

# ── replace any prior agent-tools uv-tool install ──────────────────────────────
# `dev` previously shipped as the `agenttools-dev` uv tool (a symlink at $BIN/dev → uv's tools
# dir). Uninstall it so the symlink below is not shadowed / left dangling. Best-effort.
if command -v uv >/dev/null 2>&1; then
  if uv tool list 2>/dev/null | grep -q '^agenttools-dev'; then
    echo "dev: removing the superseded 'agenttools-dev' uv tool"
    uv tool uninstall agenttools-dev >/dev/null 2>&1 || true
  fi
fi

# ── symlink entry ─────────────────────────────────────────────────────────────
ENTRY_PATH="$SRC/$ENTRY"
chmod +x "$ENTRY_PATH"
ln -sfn "$ENTRY_PATH" "$BIN/$TOOL"
echo "dev: symlinked $BIN/$TOOL -> $ENTRY_PATH"

# ── wire this repo's own commit gate (rules.yaml quorum) ───────────────────────
# Only meaningful for a git checkout of dev-cli itself ($SRC), never for a project that
# merely consumes `dev` at runtime. scripts/git-hooks/pre-commit blocks a commit that
# touches rules.yaml unless the task code has a passed review-cli quorum — see AGENTS.md
# "rules.yaml requires review quorum" and scripts/git-hooks/check_rules_quorum.py.
#
# Wired via a WORKTREE-SCOPED `core.hooksPath` (`extensions.worktreeConfig=true` +
# `git config --worktree core.hooksPath scripts/git-hooks`) pointed at the in-repo,
# committed scripts/git-hooks/ dir. Two things were tried and rejected first, both verified
# live during development, not just reasoned about:
#   1. A plain (non---worktree) `git config core.hooksPath` — this setting lives in the
#      REPO-SHARED .git/config, so setting it in one worktree changed it for every sibling
#      worktree of dev-cli, including the main checkout (which doesn't have
#      scripts/git-hooks/ checked out on every branch). `--worktree` scopes the value to
#      $GIT_DIR/config.worktree instead, with no such leak.
#   2. Installing directly into this checkout's worktree-specific hook slot
#      ($(git rev-parse --absolute-git-dir)/hooks/pre-commit) — this looked promising (each
#      worktree gets its own $GIT_DIR/hooks/) but vanilla git only reads hooks from
#      $GIT_COMMON_DIR/hooks/ or core.hooksPath; a linked worktree's own hooks/ subdirectory
#      is never consulted. Confirmed by writing an always-fail hook there and committing:
#      git accepted the commit as if the hook didn't exist.
# `core.hooksPath` (worktree-scoped) is the one mechanism that is both git-native and
# correctly isolated per worktree.
# Guard against a non-git $SRC (e.g. an extracted tarball snapshot that still has
# scripts/git-hooks/ present but no .git at all) — without this, `git config` inside the
# subshell below fails under `set -e` mid-install, after the dev symlink is already created
# but before install-skill runs, with a cryptic "fatal: not in a git directory" instead of a
# clean skip.
if [[ -f "$SRC/scripts/git-hooks/pre-commit" ]] && git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  chmod +x "$SRC/scripts/git-hooks/pre-commit" "$SRC/scripts/git-hooks/pre-merge-commit" \
    "$SRC/scripts/git-hooks/check_rules_quorum.py"
  (
    cd "$SRC"
    git config extensions.worktreeConfig true
    git config --worktree core.hooksPath scripts/git-hooks
  )
  echo "dev: wired $SRC's own commit gate (worktree core.hooksPath -> scripts/git-hooks)"
fi

# ── register skill ────────────────────────────────────────────────────────────
if ! "$BIN/$TOOL" install-skill; then
  echo "  WARNING: '$TOOL install-skill' failed — $TOOL is installed but agents may not"
  echo "           auto-discover it. Re-run '$TOOL install-skill' manually to fix."
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  dev is installed."
echo ""
echo "  Usage: dev run <script>          — run a named top-level rig.yaml scripts:<name>"
echo "         dev start <target>        — start a configured dev.server / dev.e2e job"
echo "         dev list | status | logs  — inspect configured + running dev/e2e targets"
echo "         dev stop <target|--pid|--port|--pgid>  — validated, project-scoped SIGTERM"
echo "         dev --help                — full usage"
echo ""
