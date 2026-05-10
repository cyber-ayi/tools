# cc-session

Persistent **tmux session** wrapper for [Claude Code](https://claude.ai/code) —
keep `claude` running across SSH disconnects, sleeps, and devices (e.g. Mac
desktop → phone over Tailscale).

Re-running `cc-session` with the same session name re-attaches to the live
session instead of starting a new one, so your conversation survives the
network blips that would otherwise drop the browser-side "Remote Control"
bridge.

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

### Recovering a Claude session

If the browser shows **"Remote Control disconnected"**, only the WebSocket
bridge to `claude.ai/code` dropped — the local transcript is still saved
on disk. Long sessions (>500k tokens) and sleep / network blips are the
usual triggers.

```bash
claude --resume <session-id>   # resume a specific session
claude --resume                # interactive picker
claude -c                      # continue most recent in this dir
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
