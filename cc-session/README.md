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

`claude` is launched with `--remote-control` by default so the session is
reachable from `claude.ai/code`. Each session cc-session creates is stamped
with the tmux user option `@cc-session-managed=1` so destructive flags
(`--teleport`) refuse to touch a same-named session you set up by hand.

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
`claude --remote-control --teleport <id>`. claude pulls the session from
the cloud, prompts to resume from summary or full transcript, and emits a
fresh Remote Control link for the browser to reconnect.

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

## License

Apache 2.0 — see [LICENSE](../LICENSE).
