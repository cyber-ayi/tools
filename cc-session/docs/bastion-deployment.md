# Bastion deployment

End-to-end walkthrough for the **VPS-as-bastion** topology: the public
internet face of `claude remote-control` lives on a VPS, while actual
work happens on your dev machine (macOS host) over SSH.

```
            ┌─────────────────────────────────────────┐
            │  claude.ai/code (browser, phone)         │
            └──────────────────┬───────────────────────┘
                              │ HTTPS via Anthropic API
                              ▼
            ┌─────────────────────────────────────────┐
            │  VPS (Servarica MTL or similar)          │
            │  cc-session — `claude remote-control`     │
            │  systemd unit: cc-session.service         │
            └──────────────────┬───────────────────────┘
                              │ Tailscale SSH (cc_bridge_ed25519)
                              ▼
            ┌─────────────────────────────────────────┐
            │  <macos-host> (workspace authority)          │
            │  ~/cc/, ~/.claude/projects/, ~/cc/.env   │
            │  Optional: launchd LaunchAgent fallback  │
            └─────────────────────────────────────────┘
```

This is the recommended setup if you want:
- **Phone / web access to claude any time** without your laptop being awake
- macOS host stays the single source of truth for workspace files (no Syncthing
  cloning, no two-machine state divergence)
- A clean DR fallback (the Mac's own cc-session) when the VPS is unreachable

For the alternative "all on the Mac" deployment, just run `cc-session -d` on
macOS host and skip this doc — but you lose phone-access-while-asleep.

## Threat model summary

See [`threat-model.md`](threat-model.md) for the full cut. One-liner: VPS
is treated as **near-zero trust** — it holds an SSH key with `from=`
Tailscale-IP restriction into the macOS host, and an Anthropic OAuth token for RC.
Both are **rotatable** if the VPS is compromised, with response time
documented in [Jarvie8176/tools#24](https://github.com/Jarvie8176/tools/issues/24).
`~/cc/.env` (the actual secret values) **stays on the Mac**.

## Prerequisites

- Ubuntu / Debian VPS reachable via Tailscale (this guide assumes Servarica MTL)
- a Mac (or other macOS host) running, on the same tailnet
- `node` + `npm` on the VPS (`apt install nodejs npm` is enough; nvm also works)
- An active Claude Pro / Max / Team / Enterprise subscription
- `gh` CLI on either machine for the manual SSH key paste step

## Step 1 — Bootstrap VPS

SSH into the VPS, clone the tools repo, run the bootstrap script:

```bash
ssh me@servarica.tail4a8253.ts.net
git clone https://github.com/Jarvie8176/tools.git ~/tools
~/tools/cc-session/install/setup-vps.sh
```

This installs `zsh tmux bats`, installs the `claude` CLI via npm,
symlinks `cc-session` into `~/.local/bin`, and walks you through
`claude auth login` (the OAuth dance that needs your browser).

## Step 2 — Generate VPS→macOS host SSH bridge key

Still on the VPS:

```bash
~/tools/cc-session/install/setup-bridge-key.sh
```

Output ends with the exact `authorized_keys` line you should paste on
macOS host, including the `from="<vps-tailscale-ip>"` IP restriction.

Switch to the Mac (or another existing SSH session into the macOS host), append the
line:

```bash
ssh me@<macos-host>.your-tailnet.ts.net
cat >> ~/.ssh/authorized_keys <<'KEY'
from="100.126.89.3",no-port-forwarding,no-X11-forwarding ssh-ed25519 AAA... cc-bridge@vps
KEY
```

Verify the bridge from VPS:

```bash
ssh -i ~/.ssh/cc_bridge_ed25519 me@<macos-host>.your-tailnet.ts.net 'echo ok && hostname'
```

Should print `ok` + your Mac's hostname.

Update [`ssh-manifest`](https://github.com/Jarvie8176/ssh-manifest) per its
own rotation procedure: new entry in `inventory.yaml`, append to
`rotation-log.md`, run `audit-keys.sh` clean.

## Step 3 — Install systemd unit on VPS

Copy the template, edit the placeholders, enable:

```bash
mkdir -p ~/.config/systemd/user
cp ~/tools/cc-session/install/systemd-vps.service.template \
   ~/.config/systemd/user/cc-session.service

$EDITOR ~/.config/systemd/user/cc-session.service   # adjust paths if needed

systemctl --user daemon-reload
systemctl --user enable --now cc-session
systemctl --user status cc-session
```

For boot-time start (not just at user login):

```bash
sudo loginctl enable-linger $USER
```

Verify cc-session has spawned:

```bash
cc-session --status
```

Should print `alive: yes`, `managed: yes`, the captured RC URL, and an
`uptime_seconds` value.

## Step 4 — Install LaunchAgent on the Mac (optional DR fallback)

Only useful if you want macOS host to spawn its own cc-session at user login,
to provide a fallback RC entry when the VPS is unreachable. Skip if
you're happy with "VPS is the only RC entry; if it's down, I'll cope."

```bash
cp ~/tools/cc-session/install/launchd-macos.plist.template \
   ~/Library/LaunchAgents/me.cc-session.plist

$EDITOR ~/Library/LaunchAgents/me.cc-session.plist   # USERNAME placeholder

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/me.cc-session.plist
launchctl print gui/$(id -u)/me.cc-session
```

Note: this will spawn a **separate** RC URL on the Mac, distinct from the
VPS's URL. Bookmark both in your phone:

- `vps-url` → primary, always-on
- `macOS host-url` → fallback, valid only when macOS host is awake

## Step 5 — End-to-end test

From your phone or a browser on a different machine:

1. Open `claude.ai/code`
2. The VPS's RC session should appear in the session list, named like
   `<hostname>-<adjective>-<noun>` (e.g., `servarica-mtl-graceful-unicorn`)
3. Click in, send a prompt that needs macOS host data, e.g.
   `What's the most recent commit message in ~/cc?`
4. claude on the VPS uses the Bash tool with `ssh me@<macos-host>.your-tailnet.ts.net 'cd ~/cc && git log -1 --format=%s'`
5. The reply should reflect the Mac's actual commit log

If step 4 fails with "Permission denied (publickey)", the SSH bridge
isn't set up correctly — re-run Step 2 verification.

## Operations

### Status

```bash
ssh me@servarica 'cc-session --status'        # VPS-side check
ssh me@<macos-host>      'cc-session --status'         # macOS host fallback check
```

### Restart

```bash
ssh me@servarica 'systemctl --user restart cc-session'
```

If the cc-session script itself was updated:

```bash
ssh me@servarica 'cd ~/tools && git pull && systemctl --user restart cc-session'
```

### Logs

```bash
ssh me@servarica 'journalctl --user -u cc-session -f'   # VPS systemd
ssh me@<macos-host>       'tail -f /tmp/cc-session.{out,err}.log' # macOS host launchd
```

### Stop

```bash
ssh me@servarica 'systemctl --user stop cc-session'
ssh me@servarica 'cc-session --status'   # confirm alive: no
```

### Recover from "Remote Control disconnected"

The VPS's RC URL became unresponsive in the browser:

```bash
# Grab the session_xxx from the browser URL bar; ssh into VPS:
ssh me@servarica
cc-session --teleport https://claude.ai/code/session_01XXXXXXXXX
# Or, if you only have the local UUID (cloud session_xxx is lost):
cc-session --resume <on-disk-uuid>
```

Both register a fresh RC URL. Update the phone bookmark.

## Failure modes + responses

| Symptom | Likely cause | Action |
|---|---|---|
| `cc-session --status` on VPS shows `alive: no` | systemd hit StartLimit; log shows repeated restart | `journalctl --user -u cc-session -n 50` to diagnose; common cause = Anthropic OAuth expired (`claude auth login` again) |
| Phone shows "Remote Control disconnected" but VPS `--status` shows `alive: yes` | Anthropic-side bridge timeout | `cc-session --teleport <url>` to re-register |
| VPS-claude can't SSH to the Mac | macOS host Tailscale IP changed, or VPS Tailscale IP changed (breaks the `from=` clause) | re-run `setup-bridge-key.sh`, update the Mac's `authorized_keys` |
| macOS host asleep, VPS RC works but file ops fail | by design — wake the Mac via `wake on lan` or external power | rare; in practice macOS host wakes from Tailscale incoming traffic |
| Suspected VPS compromise | per [#24](https://github.com/Jarvie8176/tools/issues/24) playbook | rotate every `~/cc/.env` token + revoke VPS OAuth + reprovision |

## What's NOT covered here

- **LiteLLM model router**: lives in [`agent-manifest`](https://github.com/Jarvie8176/agent-manifest)`/infra/litellm/`. Deploy via Portainer stack on the VPS.
- **Multi-agent coordination**: see `agent-manifest`'s `inventory.yaml` for agent registry + memory namespacing.
- **Memory git workflow**: see `agent-manifest`'s `docs/conventions.md`.

This doc is intentionally cc-session-focused. The broader "homelab agent
stack" lives in agent-manifest.
