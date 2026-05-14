#!/usr/bin/env bash
# Generate an ed25519 SSH key on this host (typically the VPS) for the
# cc-session bastion bridge — the key the VPS uses to SSH into the macOS host for
# tasks needing local data.
#
# Designed to run on the VPS. The pubkey is printed at the end; you
# manually paste it into the Mac's ~/.ssh/authorized_keys with a `from=`
# IP restriction to constrain blast radius.
#
# Why manual paste vs. ssh-copy-id:
#   - ssh-copy-id requires interactive password to the Mac, which the
#     bastion deployment may not have configured (the Mac typically only
#     accepts pubkey auth)
#   - The from= clause must be added by hand anyway — typing it
#     while copying ensures you don't forget it
#   - Manual paste is the security-minded path: you see exactly what
#     you're authorizing, macOS side
#
# Usage:
#   ssh me@vps
#   ~/tools/cc-session/install/setup-bridge-key.sh

set -euo pipefail

PROG="${0##*/}"

log()  { printf '\n[%s] %s\n' "$PROG" "$*"; }
fail() { printf '\n[%s] ERROR: %s\n' "$PROG" "$*" >&2; exit 1; }

KEY_PATH="$HOME/.ssh/cc_bridge_ed25519"
KEY_COMMENT="cc-bridge@$(hostname --fqdn 2>/dev/null || hostname)"

# --- 1. Generate key (or report existing) -------------------------------

if [[ -f "$KEY_PATH" ]]; then
  log "Bridge key already exists at $KEY_PATH"
  log "Pubkey:"
  cat "$KEY_PATH.pub"
else
  log "Generating ed25519 keypair at $KEY_PATH (no passphrase — for unattended SSH)"
  ssh-keygen -t ed25519 -f "$KEY_PATH" -C "$KEY_COMMENT" -N "" -q
  chmod 600 "$KEY_PATH"
fi

# --- 2. Detect Tailscale IP for from= restriction ----------------------

if command -v tailscale >/dev/null 2>&1; then
  TAILSCALE_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  if [[ -n "$TAILSCALE_IP" ]]; then
    log "Detected Tailscale IPv4: $TAILSCALE_IP"
    log "Use this in the from= clause when authorizing on the Mac."
  else
    log "WARNING: tailscale CLI present but couldn't read IP. Run 'tailscale up' first?"
    TAILSCALE_IP=""
  fi
else
  log "WARNING: Tailscale not installed. The from= IP restriction is the bastion model's main blast-radius defense; install + configure Tailscale before deploying."
  TAILSCALE_IP="<vps-tailscale-ip>"
fi

# --- 3. Print the line to add on the Mac -----------------------------------

PUBKEY="$(cat "$KEY_PATH.pub")"

cat <<EOF


===============================================================================
  NEXT STEP — paste this line into the Mac's ~/.ssh/authorized_keys:
===============================================================================

  from="${TAILSCALE_IP}",no-port-forwarding,no-X11-forwarding ${PUBKEY}

The from= clause is the load-bearing security control: even if the
private key is exfiltrated from this VPS, it's only usable from a
host carrying that exact Tailscale IP — which an attacker outside
your tailnet can't spoof.

The no-port-forwarding / no-X11-forwarding clauses are belt-and-
suspenders against using the SSH session for tunneling.

===============================================================================
  TO PASTE ON macOS host:
===============================================================================

From your laptop (or an existing SSH session into the macOS host):

  ssh me@<macos-host>.your-tailnet.ts.net   # or your Mac's tailnet hostname
  cat >> ~/.ssh/authorized_keys <<'KEY'
  from="${TAILSCALE_IP}",no-port-forwarding,no-X11-forwarding ${PUBKEY}
  KEY

Then verify from THIS VPS:

  ssh -i $KEY_PATH me@<macos-host>.your-tailnet.ts.net 'echo bridge-ok && hostname'

Should print "bridge-ok" + the Mac's hostname.

===============================================================================
  AGENT-MANIFEST INTEGRATION
===============================================================================

Add this fingerprint to ssh-manifest's inventory.yaml under the
appropriate node entry, and append to rotation-log.md so the audit
script can detect it:

  Fingerprint: $(ssh-keygen -lf "$KEY_PATH.pub" 2>/dev/null | awk '{print $2}')

EOF
