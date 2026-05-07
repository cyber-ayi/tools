#!/usr/bin/env bash
# Uninstall rclone-migrate.
#
# Removes the rmig CLI but PRESERVES user state at
# ${XDG_DATA_HOME:-~/.local/share}/rclone-migrate/ (job state.db, hash caches,
# audit logs). Delete those manually if you want a clean wipe.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/uninstall.sh | bash

set -euo pipefail

PKG_NAME="rclone-migrate"

if [ -t 1 ]; then
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    RESET=$(printf '\033[0m')
else
    GREEN=""
    YELLOW=""
    RESET=""
fi
log()  { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%swarn:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }

if ! command -v pipx >/dev/null 2>&1; then
    warn "pipx not found. If $PKG_NAME was installed another way, remove it manually:"
    warn "  pip uninstall $PKG_NAME"
    exit 1
fi

if ! pipx list --short 2>/dev/null | awk '{print $1}' | grep -qx "$PKG_NAME"; then
    warn "$PKG_NAME is not installed via pipx. Nothing to uninstall."
    if command -v rmig >/dev/null 2>&1; then
        warn "However, 'rmig' is on PATH at: $(command -v rmig)"
        warn "Investigate manually; may have been installed via 'pip install --user'."
    fi
    exit 0
fi

log "Removing $PKG_NAME (pipx)..."
pipx uninstall "$PKG_NAME" >/dev/null

# Inform about preserved state — never delete it.
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/rclone-migrate"
if [ -d "$STATE_DIR" ]; then
    warn "User state preserved at: $STATE_DIR"
    warn "Remove manually if you no longer need it: rm -rf '$STATE_DIR'"
fi

log "Done."
