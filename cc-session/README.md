# cc-session

Persistent **tmux session** wrapper for [Claude Code](https://claude.ai/code) —
keep `claude` running across SSH disconnects, sleeps, and devices (e.g. Mac
desktop → phone over Tailscale).

Re-running `cc-session` with the same session name re-attaches to the live
session instead of starting a new one, so your conversation survives the
network blips that would otherwise drop the browser-side "Remote Control"
bridge.

When the browser bridge does drop ("Remote Control disconnected"), copy the
`session_xxx` URL from the browser and run
`cc-session --teleport <id-or-url>` — see [Recovering a stuck Remote Control
session](#recovering-a-stuck-remote-control-session) below.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/cc-session/cc-session -o ~/bin/cc-session
chmod +x ~/bin/cc-session
```

Requires `tmux` and the `claude` CLI on `PATH` (or pointed to via
`CLAUDE_BIN`).

## Usage

```bash
cc-session                          # ~/cc, session 'claude'
cc-session ~/work/api               # custom project, session 'claude'
cc-session ~/work/api api           # custom project, session 'api'
cc-session -d ~/work/api api        # start 'api' detached, return to shell
cc-session api                      # later: attach to 'api'
cc-session --list                   # show running sessions
cc-session --kill claude            # terminate the 'claude' session
cc-session --help                   # full reference
```

After launch, the URL `claude.ai/code` should open the conversation
on is captured from the pane and written to
`$TMPDIR/cc-session/<SESSION_NAME>.url`, plus flashed in the tmux
status bar via `tmux display-message`.

Each session cc-session creates is stamped with the tmux user option
`@cc-session-managed=1` so destructive flags (`--teleport`, `--adopt`)
refuse to touch a same-named session you set up by hand.

### Launch modes

| Mode                | Tmux pane command           | URL shape                                       |
|---------------------|-----------------------------|-------------------------------------------------|
| Default (v0.3.0+)   | `claude remote-control`     | `https://claude.ai/code?environment=env_<id>`   |
| `--teleport <id>`   | `claude --teleport <id>`    | `https://claude.ai/code/session_<id>`           |

The default launch runs the dedicated `claude remote-control`
subcommand — a persistent multi-session server that prints its URL
on startup. No keystrokes are scripted into the pane.

The `--teleport` path is different: `claude remote-control` doesn't
accept a teleport id, so cc-session launches the interactive
`claude --teleport <id>` and bridges it to claude.ai/code by sending
`/remote-control` as a slash command once the TUI reaches an idle
prompt.

> Prior versions (≤ 0.2.x) ran a bare `claude` interactive TUI in
> tmux and used `/remote-control` for *every* launch. That path left
> a dangling JSONL session per launch in `~/.claude/projects/...`
> (the TUI's original session was abandoned the moment the slash
> command transitioned it away). The server-mode default in 0.3.0+
> avoids the orphan.

## Bastion deployment (VPS RC entry, mbp data plane)

For the topology where a Tailscale-reachable VPS hosts the public
`claude.ai/code` URL and SSHes into mbp for tasks needing local data:

- Install templates in [`install/`](install/):
  - [`launchd-mbp.plist.template`](install/launchd-mbp.plist.template) — mbp DR fallback
  - [`systemd-vps.service.template`](install/systemd-vps.service.template) — VPS primary
  - [`setup-vps.sh`](install/setup-vps.sh) — bootstrap script for fresh VPS
  - [`setup-bridge-key.sh`](install/setup-bridge-key.sh) — VPS→mbp SSH bridge keypair
- End-to-end walkthrough: [`docs/bastion-deployment.md`](docs/bastion-deployment.md)
- Threat model: [`docs/threat-model.md`](docs/threat-model.md)

## Tips

The first pane runs `claude` directly with no shell. To get a shell without
killing claude:

| Inside tmux  | Action                                  |
|--------------|-----------------------------------------|
| `prefix d`   | detach (claude keeps running)           |
| `prefix c`   | new window — gives you a shell          |
| `prefix \|` / `prefix -` | split pane (custom binding) |
| `prefix n` / `prefix p`  | next / previous window      |
| `prefix [`   | scroll mode (`q` to exit)               |

Or from outside: `tmux new-window -t <SESSION_NAME>`.

## Use case: reclaim an orphaned cloud session

You're working on `claude.ai/code/session_xxx` from a browser; mid-turn,
the page changes to:

> **Remote Control disconnected**
> Your terminal's Claude Code session stopped responding. Check your
> terminal for errors, then resend your message.

The local `claude` process can no longer talk to the cloud-side bridge.
The conversation **transcript is not lost** — it's persisted both
on-disk under `~/.claude/projects/.../<uuid>.jsonl` and in the cloud —
but the browser tab is now bound to a bridge that won't recover even
with a refresh. The session is *orphaned*: alive in storage, but
unreachable from any UI.

`cc-session --teleport` reclaims it by pulling the cloud transcript
into a fresh local claude process and registering a new RC URL.

### Steps

**1. From the browser, copy the orphaned session URL.**

It's the address bar of the disconnected tab:

```
https://claude.ai/code/session_01EXAMPLEabcdef1234567890
```

(Or right-click the session in the sidebar → "Copy link", if your
client surfaces that.)

**2. From a terminal on the host running cc-session** (likely via SSH
+ tmux from another device):

```bash
cc-session --teleport https://claude.ai/code/session_01EXAMPLEabcdef1234567890
```

Three input shapes are accepted; cc-session canonicalizes them all to
`session_<id>`:

```bash
cc-session --teleport https://claude.ai/code/session_01EXAMPLEabcdef1234567890   # full URL
cc-session --teleport session_01EXAMPLEabcdef1234567890                          # bare ID
cc-session --teleport 01EXAMPLEabcdef1234567890                                  # suffix only
```

cc-session will (in this order):

1. Kill the same-named tmux session **only if** it was created by
   cc-session (`@cc-session-managed=1`); recreate it fresh.
2. Launch `claude --teleport <id>`, which fetches the cloud transcript.
3. Wait for the **"Resume from summary / Resume full session as-is"**
   prompt and auto-pick option 1 (summary). Override with `--full` if
   you really want to re-pay the full token cost (asks for `yes`
   confirmation; bypass with `CC_SESSION_SKIP_FULL_CONFIRM=1`).
4. Once claude is back at an idle prompt, send `/remote-control` to
   register a new Remote Control URL with `claude.ai/code`.
5. Write the new URL to `$TMPDIR/cc-session/<SESSION_NAME>.url` and
   flash it in the tmux status bar via `tmux display-message`.

**3. Open the new URL** (the one cc-session printed) in your browser.
The conversation is back, prefaced by a "Session resumed" marker and a
summary of the prior context.

> ⚠️ **The original `session_xxx` URL stays bound to its (now-dead)
> bridge.** Refreshing the old browser tab keeps showing the disconnect
> message — that URL is essentially a pointer to the bridge process,
> not the conversation. The new URL is the only live one going forward.

### Variations

```bash
# Resume the FULL transcript (re-pays all tokens; prompts for "yes"):
cc-session --teleport <url> --full

# Auto-/compact after teleport to free context for follow-up work
# (fires shortly after the new RC URL is captured — no fixed delay
# in 0.4+; the cc-session script knows when claude has reached idle):
cc-session --teleport <url> --compact

# A cc-session-managed session is already running but RC isn't visible
# in claude.ai/code (e.g. /remote-control was never invoked, or you
# disconnected it earlier) — just register a fresh RC URL on the spot:
cc-session --adopt                  # default 'claude' tmux session
cc-session --adopt my-session       # named session
```

`--adopt` is idempotent: if RC is already active (always the case for
default-launched server-mode sessions; usually the case for previously
adopted teleport sessions), it just prints the existing URL by
reading the pane scrollback. Only falls through to a `/remote-control`
keystroke when the URL isn't already visible in the pane.

### What if the URL is "lost"?

If you can't get the orphaned URL from the browser — tab was closed,
session in private window with cleared history, etc. — there is no
local equivalent. The on-disk session UUID
(`~/.claude/projects/.../<uuid>.jsonl`) is a *different ID space* from
the cloud `session_xxx` and can't be used with `--teleport`. You'd
fall back to:

```bash
claude --resume <on-disk-uuid>     # local-only resume; no RC URL
claude --resume                    # interactive picker
```

— and then enable RC manually with `cc-session --adopt` once that
claude is up.

## Avoiding sleep-induced drops

On macOS, `caffeinate -i` blocks idle sleep for the lifetime of the wrapped
process:

```bash
caffeinate -i cc-session              # session stays alive while attached
caffeinate -i -t 7200 cc-session -d   # detach, hold awake for 2h
```

Lid-closed (clamshell) sleep is not covered by `caffeinate` and needs
external power + display + input, or a system-settings change.

## Tests

Black-box tests live in [`tests/`](tests/) and run under
[bats](https://github.com/bats-core/bats-core). They stub the `claude`
binary with `tests/fixtures/fake-claude` so nothing touches the real CLI
or the cloud.

```bash
brew install bats-core         # macOS
sudo apt install bats          # Ubuntu

bats cc-session/tests/         # 32 tests, ~10s
```

CI (`.github/workflows/ci.yml`) runs the same suite plus a
`zsh -n cc-session` syntax check on every PR that touches `cc-session/`.

## License

Apache 2.0 — see [LICENSE](../LICENSE).
