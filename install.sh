#!/usr/bin/env bash
# install.sh — install the `dev` CLI (Python 3).
# Works from a local clone (./install.sh) and piped from curl:
#   curl -fsSL https://raw.githubusercontent.com/alex-mextner/dev-cli/main/install.sh | bash
set -euo pipefail

# ── identity ──────────────────────────────────────────────────────────────────
TOOL="dev"
REPO="dev-cli"
GITHUB_USER="alex-mextner"
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
  EXPECT_URL="https://github.com/$GITHUB_USER/$REPO.git"
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
