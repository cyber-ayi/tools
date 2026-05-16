#!/usr/bin/env bash
# Bootstrap a fresh VPS for the cc-session bastion deployment.
#
# What this script does:
#   1. Installs zsh, tmux, bats (apt — Debian/Ubuntu only)
#   2. Installs claude CLI (via npm — assumes node + npm already present)
#   3. Symlinks cc-session into ~/.local/bin
#   4. Walks user through `claude auth login` (interactive)
#   5. Prints the systemd unit install commands
#
# What this script does NOT do (require human attention):
#   - claude OAuth flow needs your browser; script blocks for you
#   - SSH key from VPS to the Mac (use install/setup-bridge-key.sh after)
#   - systemd unit install (template printed; you copy + edit)
#   - LiteLLM stack (lives in agent-manifest, not cc-session)
#
# Prerequisites:
#   - Ubuntu / Debian VPS reachable via Tailscale
#   - node + npm available (apt install nodejs npm OR nvm)
#   - cc-session repo cloned locally (this script lives inside it)
#
# Usage:
#   ssh me@vps
#   git clone https://github.com/Jarvie8176/tools.git ~/tools
#   ~/tools/cc-session/install/setup-vps.sh

set -euo pipefail

PROG="${0##*/}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC_SESSION="$SCRIPT_DIR/../cc-session"

# Pin claude version. 2.1.141 added a workspace-trust dialog and an
# `Enable Remote Control? (y/n)` confirmation in front of `claude
# remote-control` startup, both of which cc-session's unattended polling
# can't navigate. 2.1.114 is the last known-good version for the bastion
# (server-mode default launch). Bump deliberately after re-testing
# `cc-session --status` against a fresh systemd start.
CLAUDE_VERSION="${CLAUDE_VERSION:-2.1.114}"

log()  { printf '\n[%s] %s\n' "$PROG" "$*"; }
fail() { printf '\n[%s] ERROR: %s\n' "$PROG" "$*" >&2; exit 1; }

# --- 1. apt deps ------------------------------------------------------------

if ! command -v apt-get >/dev/null 2>&1; then
  fail "apt-get not found — this script targets Debian/Ubuntu. For other distros, install zsh + tmux + bats manually."
fi

log "Installing zsh, tmux, bats via apt"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends zsh tmux bats curl
zsh --version
tmux -V
bats --version

# --- 2. claude CLI --------------------------------------------------------

if ! command -v npm >/dev/null 2>&1; then
  fail "npm not found — install node + npm first (apt install nodejs npm OR use nvm)."
fi

current_claude_version=""
if command -v claude >/dev/null 2>&1; then
  current_claude_version="$(claude --version 2>/dev/null | awk '{print $1}')"
fi

if [[ "$current_claude_version" == "$CLAUDE_VERSION" ]]; then
  log "claude already at pinned version $CLAUDE_VERSION"
else
  if [[ -n "$current_claude_version" ]]; then
    log "claude $current_claude_version installed, replacing with pinned $CLAUDE_VERSION"
  else
    log "Installing claude@${CLAUDE_VERSION} via npm"
  fi
  npm install -g "@anthropic-ai/claude-code@${CLAUDE_VERSION}"
  command -v claude >/dev/null 2>&1 || fail "claude install succeeded but binary not on PATH"
fi

# --- 3. symlink cc-session into ~/.local/bin -----------------------------

mkdir -p "$HOME/.local/bin"
if [[ ! -x "$CC_SESSION" ]]; then
  fail "cc-session script not executable at $CC_SESSION"
fi
ln -sf "$CC_SESSION" "$HOME/.local/bin/cc-session"
log "Symlinked: $HOME/.local/bin/cc-session -> $CC_SESSION"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) : ;;
  *) log "WARNING: $HOME/.local/bin not in PATH. Add to ~/.zshrc / ~/.bashrc:
    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# --- 4. claude auth login (interactive) ----------------------------------

log "Next step: claude auth login (OAuth flow — needs your browser)"
cat <<'EOF'

You need to run `claude auth login` interactively from THIS terminal:

  1. Below, claude will print a URL.
  2. Open the URL in a browser ON A MACHINE WHERE YOU'RE SIGNED INTO claude.ai
     (probably your laptop, NOT this VPS).
  3. Authorize the device. Browser will show a code.
  4. Paste the code back into this terminal.
  5. claude saves an OAuth token under ~/.claude/auth.json.
  6. The token survives container/process restarts but lives only on this
     VPS. If you redeploy the container, you'll re-do this step.

WHY NOT `claude setup-token`:
   setup-token issues a long-lived token but it is INFERENCE-ONLY — it
   cannot establish Remote Control sessions. Claude Code's binary will
   error with "Remote Control requires a full-scope login token" if you
   try. Use OAuth (claude auth login).

Press Enter to start `claude auth login`, or Ctrl-C to do it yourself
later.
EOF
read -r _

claude auth login

# --- 5. systemd unit install hint ----------------------------------------

log "All dependencies + cc-session in place. To enable cc-session as a
boot-persistent systemd unit, copy + edit the template:

  mkdir -p ~/.config/systemd/user
  cp $SCRIPT_DIR/systemd-vps.service.template ~/.config/systemd/user/cc-session.service
  \$EDITOR ~/.config/systemd/user/cc-session.service   # adjust paths

Then enable + start:

  systemctl --user daemon-reload
  systemctl --user enable --now cc-session
  systemctl --user status cc-session
  journalctl --user -u cc-session -f

For start-at-boot (not just at user login):
  sudo loginctl enable-linger \$USER

Next steps after this script:
  - install/setup-bridge-key.sh   — generate SSH key for VPS->macOS host tasks
  - docs/bastion-deployment.md     — end-to-end deployment walkthrough
"
