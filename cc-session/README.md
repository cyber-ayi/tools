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

**Recommended (git clone)** — required for `cc-session --update` to work:

```bash
git clone https://github.com/Jarvie8176/tools.git ~/Github/tools
ln -sf ~/Github/tools/cc-session/cc-session ~/bin/cc-session
```

**Single-file (curl)** — lightweight, but `--update` is unavailable:

```bash
rm -f ~/bin/cc-session   # break any pre-existing symlink first; otherwise
                         # `curl -o` follows the symlink and clobbers the
                         # source-of-truth file in your git checkout.
curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/cc-session/cc-session -o ~/bin/cc-session
chmod +x ~/bin/cc-session
```

Runtime requirements:
- `zsh` (the script's `#!/bin/zsh` shebang)
- `tmux`
- `claude` CLI on `PATH` (or pointed to via `CLAUDE_BIN` env var)

Per-OS install:

```bash
# macOS — zsh ships with the OS; only tmux to install:
brew install tmux

# Debian / Ubuntu / WSL2:
sudo apt install -y zsh tmux

# Fedora / RHEL:
sudo dnf install -y zsh tmux
```

CI exercises the bats suite on **both** Ubuntu and macOS runners — see
`.github/workflows/ci.yml`'s `cc-session (bats / <os>)` job.

## Updating

From 0.5.1 onwards, `cc-session` self-updates:

```bash
cc-session --update            # interactive: prompts before fast-forwarding
cc-session --update --check    # dry-run: list pending upstream commits
```

Self-update requires `cc-session` to live inside a `git clone` of this
repo (it fast-forwards the local checkout via `git merge --ff-only`),
and refuses to apply when the working tree is dirty, when the local
branch has diverged commits, or when stdin is not a tty. Set
`CC_SESSION_UPDATE_YES=1` to skip the confirmation in scripts.

### One-time bootstrap to 0.5.1

`--update` itself shipped in 0.5.1, so older installs need a single
manual step to pick it up. Pick the branch that matches your install:

```bash
# Git-clone install (recommended): just pull.
cd ~/Github/tools && git pull origin main

# curl-installed (no git checkout): re-install as a git clone so that
# --update works from now on:
git clone https://github.com/Jarvie8176/tools.git ~/Github/tools
ln -sf ~/Github/tools/cc-session/cc-session ~/bin/cc-session
```

After this one-time step, future upgrades are just `cc-session --update`.

## Usage

```bash
cc-session                          # ~/cc, session 'claude'
cc-session ~/work/api               # custom project, session 'claude'
cc-session ~/work/api api           # custom project, session 'api'
cc-session -d ~/work/api api        # start 'api' detached, return to shell
cc-session api                      # later: attach to 'api'
cc-session -w ops/foo ~/Github/homelab-ops foo
                                    # per-task git worktree off origin/main
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
refuse to touch a same-named session you set up by hand, plus
`@cc-session-mode={server,teleport}` which drives the safety state
machine described in [The @cc-session-mode state
machine](#the-cc-session-mode-state-machine).

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

## The @cc-session-mode state machine

Every managed session also carries `@cc-session-mode`. The two states
have opposite lifecycles, and conflating them is exactly the [#30
footgun](https://github.com/Jarvie8176/tools/issues/30) (a stray
`--teleport` killing a bastion that was multiplexing N live sessions).
0.6.0 makes the distinction structural:

| | `server` | `teleport` |
|---|---|---|
| Launched by | default (`claude remote-control`) | `--teleport` / `--resume` |
| Multiplexing | one process ↔ N browser-spawned sessions ("n of 32") | one process ↔ exactly one session |
| Default tmux name | `claude` | auto `claude-tp-<id8>-<rand6hex>` |
| `--teleport`/`--resume`/`--adopt` onto it | **refused** (would drop all N sessions — #30) | **refused** (single-use; `--kill` first) |
| Name collision | n/a | **hard error**, never a silent recycle/kill |
| Display title | as-is | `[T] ` prefix env (`CC_SESSION_TELEPORT_TITLE_PREFIX`) — see caveat |
| When claude exits | dead pane preserved (debuggable) | auto-reaped (cleans UI noise) |
| Audited | no | birth + URL-capture + reap → audit log |

This means **a bare `cc-session --teleport <id>` can no longer destroy
a server-mode RC**: with no explicit name it is auto-named to something
that cannot collide, and even an explicit collision is a hard error
rather than the old "kill the same-named session and recreate it".

> **`[T] ` caveat (servarica e2e):** the prefix is exported as
> `CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX` to the teleport claude,
> but the bastion e2e confirmed it does **not** surface in the
> claude.ai/code FleetView title for teleported sessions — preserving
> or exposing a resumed session's title is an upstream
> (`anthropics/claude-code`) gap. The prefix is kept as a harmless,
> correct-by-construction local/terminal hint; the FleetView
> disambiguation is tracked for the deferred upstream issue.

### Auto-reaper

A teleport session is short-lived by nature — you revive one orphan,
finish, done. Once its `claude` exits (dead pane), cc-session reaps the
tmux session so it stops lingering as zombie UI noise. Tunables:

```bash
CC_SESSION_NO_REAP=1         # disable; preserve the dead pane like server-mode
CC_SESSION_REAP_GRACE=3      # seconds to re-confirm a dead pane before reaping
CC_SESSION_REAP_IDLE_MIN=0   # opt-in: also reap after N idle minutes (0 = off)
```

(A clean local reap does **not** instantly deregister the cloud-side
lease — that lease ages out on its own; this just stops the *local*
tmux clutter and shortens how long the entry looks live.)

### Revive audit

Teleport/resume lineage is appended to a durable, local-only JSONL log
(default `${XDG_STATE_HOME:-~/.local/state}/cc-session/audit.jsonl`,
override with `CC_SESSION_AUDIT_FILE`): the requested orphan id, the
tmux name, and the derived `env_`/`session_` id once the new RC URL is
captured. This is what answers "which new id did my old session become"
after the fact. The audit is best-effort — a read-only state dir never
fails a launch.

### Operational rule (not enforced by code)

For routine multi-session work, open new sessions from the **server-mode
environment's URL** — that path already multiplexes cleanly as
n-of-capacity with no ambiguity. Reserve `--teleport` strictly for
reviving one specific orphaned session. The picker ambiguity that
motivated this whole state machine only ever arises on the teleport
path.

## Per-task worktree (`--worktree`)

In a shared clone operated by multiple concurrent agents (Claude Code
sessions, cyber-ayi, hermes), `git checkout -b` in the main clone is a
race: every process sees the same `HEAD`, and a commit can land on
whatever branch was checked out most recently. `--worktree NAME`
sidesteps this by creating a fresh git worktree off `origin/main`
(override base ref with `CC_SESSION_WORKTREE_BASE`) and launching
claude inside it.

```bash
cc-session -w ops/foo ~/Github/homelab-ops foo
# →  ~/Github/homelab-ops-wt/foo/   (branch ops/foo, off origin/main)
```

Layout convention: `<parent-of-repo>/<repo>-wt/<tail-of-NAME>/`. Branch
gets the exact `NAME` (so `-w ops/foo` produces branch `ops/foo`). If
the repo carries `scripts/bootstrap.sh` (e.g. `homelab-ops` Layer 1
pre-push hook), cc-session runs it inside the worktree automatically.

Cleanup after the task closes:

```bash
git worktree remove ../homelab-ops-wt/foo
git worktree prune
```

Compatible with `--teleport` / `--resume` / `--detach`.

### Startup hint (non-main branch detection)

If you launch `cc-session` without `-w` / `--teleport` / `--resume` and
PROJECT_DIR turns out to be a git repo whose HEAD is on a non-main branch,
cc-session prints a one-time hint on stderr suggesting either:

- (a) isolate the new task via `-w ops/<task>`, **or**
- (b) reset the main clone back to main if the prior task is done.

This catches the two common pathologies in shared multi-agent clones: a
new task starting without isolation, and a finished task leaving the main
clone parked on a feature branch. Suppress with `CC_SESSION_NO_WORKTREE_HINT=1`.

## Bastion deployment (VPS RC entry, macOS host as data plane)

For the topology where a Tailscale-reachable VPS hosts the public
`claude.ai/code` URL and SSHes into the macOS host for tasks needing local data:

- Install templates in [`install/`](install/):
  - [`launchd-macos.plist.template`](install/launchd-macos.plist.template) — macOS DR fallback
  - [`systemd-vps.service.template`](install/systemd-vps.service.template) — VPS primary
  - [`setup-vps.sh`](install/setup-vps.sh) — bootstrap script for fresh VPS
  - [`setup-bridge-key.sh`](install/setup-bridge-key.sh) — VPS→macOS host SSH bridge keypair
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

1. Allocate a collision-safe tmux name `claude-tp-<id8>-<rand6hex>`
   (unless you passed an explicit name). It never reuses `claude`, so
   it cannot collide with — let alone kill — a server-mode session. A
   name collision is a hard error, never a silent recycle.
2. Append the launch to the revive audit log.
3. Launch `claude --teleport <id>`, which fetches the cloud transcript.
4. Wait for the **"Resume from summary / Resume full session as-is"**
   prompt and auto-pick option 1 (summary). Override with `--full` if
   you really want to re-pay the full token cost (asks for `yes`
   confirmation; bypass with `CC_SESSION_SKIP_FULL_CONFIRM=1`).
5. Once claude is back at an idle prompt, send `/remote-control` to
   register a new Remote Control URL with `claude.ai/code`.
6. Write the new URL to `$TMPDIR/cc-session/<SESSION_NAME>.url`, append
   the derived id to the audit log, and flash the URL in the tmux
   status bar via `tmux display-message`.

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
default-launched server-mode sessions), it just prints the existing URL
by reading the pane scrollback. Only falls through to a
`/remote-control` keystroke when the URL isn't already visible in the
pane. It **refuses a `@cc-session-mode=teleport` session** — those are
single-use and already have their URL recorded in the audit log; to
re-register one, `--kill` it and `--teleport` again.

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

bats cc-session/tests/         # full black-box suite
```

CI (`.github/workflows/ci.yml`) runs the same suite plus a
`zsh -n cc-session` syntax check on every PR that touches `cc-session/`.

## License

Apache 2.0 — see [LICENSE](../LICENSE).
