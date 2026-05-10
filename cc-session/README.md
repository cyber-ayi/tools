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

After launch, cc-session enables Remote Control by sending the
`/remote-control` slash command into the claude TUI (background subshell;
idempotent). The captured `claude.ai/code/session_xxx` URL is written to
`$TMPDIR/cc-session/<SESSION_NAME>.url` and flashed in the tmux status
bar via `tmux display-message`.

Each session cc-session creates is stamped with the tmux user option
`@cc-session-managed=1` so destructive flags (`--teleport`, `--adopt`)
refuse to touch a same-named session you set up by hand.

> Why a slash command, not the `--remote-control` startup flag? Because
> `claude --teleport <id>` silently ignores `--remote-control` at
> startup. The slash command works uniformly for both teleport and
> default launches.

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

### Recovering a stuck Remote Control session

When the browser shows **"Remote Control disconnected"**, the local
transcript is still safe — only the WebSocket bridge to `claude.ai/code`
dropped. Long sessions (>500k tokens) and sleep / network blips are the
usual triggers. Recover with:

```bash
# Copy the session URL from the browser, then:
cc-session --teleport https://claude.ai/code/session_0195UVJA1HNCupijDHF8jL7g
cc-session --teleport session_0195UVJA1HNCupijDHF8jL7g          # bare ID
cc-session --teleport 0195UVJA1HNCupijDHF8jL7g                  # suffix only

# Auto-/compact 60s after teleport (pick "summary" in between):
cc-session --teleport session_0195UVJA1HNCupijDHF8jL7g --compact

# Custom delay before /compact:
CC_SESSION_COMPACT_DELAY=120 cc-session --teleport session_xxx --compact
```

`--teleport` kills the existing tmux session (only if it carries the
`@cc-session-managed=1` marker — see above) and starts a new one running
`claude --teleport <id>`. claude pulls the session from the cloud,
prompts to resume from summary or full transcript, and once it's idle,
cc-session sends `/remote-control` to surface a *new* claude.ai/code URL.

> Note: the original `session_xxx` URL stays bound to its (now-dead)
> bridge — refreshing the old browser tab won't help. You'll get a new
> URL after teleport; cc-session writes it to `$TMPDIR/cc-session/<NAME>.url`
> and flashes it in the tmux status bar so it's easy to find.

### Adopting an already-running session

If a `cc-session`-managed tmux session is running but isn't visible in
`claude.ai/code` (e.g. you killed `/remote-control` earlier, or claude
started without RC), `--adopt` enables RC on the spot:

```bash
cc-session --adopt                  # default 'claude' tmux session
cc-session --adopt my-session-name  # custom session name
```

Idempotent — if RC is already active it just prints the existing URL.
Refuses to act on tmux sessions without the `@cc-session-managed`
marker (so it won't poke shells or non-cc-session claude instances).

cc-session **auto-picks "Resume from summary"** (option 1) at the
post-teleport prompt by polling the pane for the prompt text and sending
the keystroke once it shows. To resume the full transcript instead, pass
`--full`:

```bash
cc-session --teleport session_xxx --full
# Prompts: Type "yes" to continue with --full:
#   - because full resume can re-pay the entire conversation's tokens

# In scripts, skip the confirmation:
CC_SESSION_SKIP_FULL_CONFIRM=1 cc-session --teleport session_xxx --full -d
```

You can also resume locally without teleport, by the on-disk session UUID
(different ID space from `session_xxx`):

```bash
claude --resume <uuid>     # resume by ID
claude --resume            # interactive picker
claude -c                  # continue most recent in this dir
```

When the resume prompt offers it, prefer **"resume from summary"** over the
full transcript so you don't re-pay the entire token count.

### Avoiding sleep-induced drops

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
