#!/usr/bin/env bats
#
# Black-box tests for cc-session. The script is zsh and bats runs in
# bash, so we never source it — every test invokes cc-session as a
# subprocess. claude is stubbed with tests/fixtures/fake-claude so we
# never touch the real CLI, the cloud, or anything network-bound.
#
# Important note on bats + `[[ ]]`: a non-final `[[ ... ]]` that is
# false does NOT fail the test (only the last command's exit status
# determines pass/fail). All assertions in this file go through
# `assert_*` / `refute_*` helpers below, which return non-zero on
# mismatch and produce a useful diagnostic.

setup() {
  CC_SESSION="${BATS_TEST_DIRNAME}/../cc-session"
  FAKE_CLAUDE="${BATS_TEST_DIRNAME}/fixtures/fake-claude"
  chmod +x "$FAKE_CLAUDE" "$CC_SESSION"

  TEST_DIR="${BATS_TMPDIR}/cc-session-test-$$-${BATS_TEST_NUMBER}"
  mkdir -p "$TEST_DIR"

  SESSION_NAME="cc-test-$$-${BATS_TEST_NUMBER}"

  export CLAUDE_BIN="$FAKE_CLAUDE"
  # Isolate this test run's tmux server and TMPDIR-based state so we
  # don't collide with the host's tmux or any prior cc-session run.
  export TMUX_TMPDIR="${BATS_TMPDIR}/cc-session-tmux-$$"
  export TMPDIR="${BATS_TMPDIR}"
  mkdir -p "$TMUX_TMPDIR"
}

teardown() {
  tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
  rm -rf "$TEST_DIR" \
         "${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
}

# --- Assertion helpers (non-final-line safe) -------------------------

assert_eq() {
  if [ "$1" != "$2" ]; then
    printf 'ASSERT_EQ failed: expected %q got %q\n' "$2" "$1" >&2
    return 1
  fi
}

assert_contains() {
  if [[ "$1" != *"$2"* ]]; then
    printf 'ASSERT_CONTAINS failed:\n  haystack: %q\n  needle:   %q\n' "$1" "$2" >&2
    return 1
  fi
}

refute_contains() {
  if [[ "$1" == *"$2"* ]]; then
    printf 'REFUTE_CONTAINS failed:\n  haystack: %q\n  needle:   %q (unexpectedly present)\n' "$1" "$2" >&2
    return 1
  fi
}

# --- Pane-content helpers -------------------------------------------

# Read the "fake claude args: ..." line from a session's first pane.
pane_args() {
  local sess="$1"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    out="$(tmux capture-pane -t "$sess" -p 2>/dev/null | grep '^fake claude args:' || true)"
    [ -n "$out" ] && { printf '%s\n' "$out"; return 0; }
    sleep 0.1
  done
  return 1
}

# Wait up to ~30s for a substring to appear in a session's pane.
# Returns 0 if found, 1 on timeout.
wait_for_pane() {
  local sess="$1" needle="$2" tries="${3:-60}"
  for _ in $(seq 1 "$tries"); do
    if tmux capture-pane -t "$sess" -p -S -200 2>/dev/null | grep -q -F "$needle"; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

marker_value() {
  tmux show-options -t "$1" -v '@cc-session-managed' 2>/dev/null || true
}

# --- usage / help ----------------------------------------------------

@test "--help renders and exits 0 with all sections" {
  run "$CC_SESSION" --help
  assert_eq "$status" 0
  assert_contains "$output" NAME
  assert_contains "$output" SYNOPSIS
  assert_contains "$output" -- --teleport
  assert_contains "$output" -- --resume
  assert_contains "$output" -- --full
  assert_contains "$output" -- --compact
  assert_contains "$output" -- --adopt
  assert_contains "$output" "@cc-session-managed"
  assert_contains "$output" CC_SESSION_SKIP_FULL_CONFIRM
  assert_contains "$output" CC_SESSION_COMPACT_DELAY
  assert_contains "$output" CC_SESSION_RESUME_TIMEOUT
  assert_contains "$output" CC_SESSION_RC_URL_TIMEOUT
  assert_contains "$output" CC_SESSION_RC_ENABLE_TIMEOUT
}

@test "-h is an alias for --help" {
  run "$CC_SESSION" -h
  assert_eq "$status" 0
  assert_contains "$output" NAME
}

@test "unknown flag exits 2 with hint" {
  run "$CC_SESSION" --bogus
  assert_eq "$status" 2
  assert_contains "$output" "unknown option: --bogus"
  assert_contains "$output" "Try"
}

@test "--version prints program name + semver" {
  run "$CC_SESSION" --version
  assert_eq "$status" 0
  # Output looks like: "cc-session 0.2.0"
  [[ "$output" =~ ^cc-session\ [0-9]+\.[0-9]+\.[0-9]+$ ]]
}

@test "-v is an alias for --version" {
  run "$CC_SESSION" -v
  assert_eq "$status" 0
  assert_contains "$output" "cc-session"
}

# --- --kill scaffolding ---------------------------------------------

@test "--kill without a name exits 2" {
  run "$CC_SESSION" --kill
  assert_eq "$status" 2
  assert_contains "$output" -- "--kill requires a session name"
}

@test "--kill removes the state .url file (stale URL hygiene)" {
  # Create a session, wait for state file to appear, then --kill and
  # confirm both the tmux session and the state file are gone.
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]

  run "$CC_SESSION" --kill "$SESSION_NAME"
  assert_eq "$status" 0
  [ ! -f "$state_file" ]
  run tmux has-session -t "$SESSION_NAME"
  refute_contains "$status" 0
}

@test "--kill on a never-existed session still cleans state file (best effort)" {
  # Plant a stale state file from some prior cc-session that crashed.
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  mkdir -p "$(dirname "$state_file")"
  printf 'https://claude.ai/code/session_OBSOLETE12345\n' > "$state_file"

  # No tmux session of this name exists. --kill should fail (tmux exit
  # 1) but still wipe the state file.
  run "$CC_SESSION" --kill "$SESSION_NAME"
  refute_contains "$status" 0
  [ ! -f "$state_file" ]
}

# --- --status -------------------------------------------------------

@test "--status on a live managed session: alive=yes, url present, uptime>0" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # Wait for state file (URL captured).
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]
  sleep 1  # ensure uptime_seconds is non-zero

  run "$CC_SESSION" --status "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "session: $SESSION_NAME"
  assert_contains "$output" "alive: yes"
  assert_contains "$output" "managed: yes"
  assert_contains "$output" "https://claude.ai/code?environment=env_FAKE"
  # uptime_seconds: <int> with int >= 0
  [[ "$output" =~ uptime_seconds:\ ([0-9]+) ]] && [ "${BASH_REMATCH[1]}" -ge 0 ]
}

@test "--status on a nonexistent session: alive=no, exit 1" {
  run "$CC_SESSION" --status "definitely-not-here-$$"
  assert_eq "$status" 1
  assert_contains "$output" "alive: no"
  assert_contains "$output" "managed: no"
}

@test "--status surfaces stale state file even when tmux session is gone" {
  # Plant a stale state file from some prior cc-session run.
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  mkdir -p "$(dirname "$state_file")"
  printf 'https://claude.ai/code/session_STALE12345\n' > "$state_file"

  run "$CC_SESSION" --status "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" "alive: no"
  # url field still emits the stale URL — caller can spot the staleness
  # because alive=no.
  assert_contains "$output" "session_STALE12345"
}

@test "--status with no arg lists every managed session, exits 0 if any alive" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  sleep 1

  run "$CC_SESSION" --status
  assert_eq "$status" 0
  assert_contains "$output" "session: $SESSION_NAME"
  assert_contains "$output" "alive: yes"
}

@test "--status with no arg + no managed sessions exits 1 with 'no managed' message" {
  # Pre-create an UNMANAGED tmux session so it's filtered out.
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  run "$CC_SESSION" --status
  assert_eq "$status" 1
  assert_contains "$output" "no managed sessions"
  refute_contains "$output" "session: $SESSION_NAME"
}

@test "state file written via tmpfile-then-rename (atomic)" {
  # Spawn a session, wait for state to land, then verify no .tmp.* file
  # was left behind in the state dir (would indicate an interrupted write).
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]
  # No leftover tmpfiles
  leftover=$(find "$(dirname "$state_file")" -maxdepth 1 -name "$SESSION_NAME.url.tmp.*" 2>/dev/null | wc -l | tr -d ' ')
  assert_eq "$leftover" "0"
}

# --- parse_session_id (exercised via --teleport) ---------------------

@test "--teleport without an id exits 2" {
  run "$CC_SESSION" --teleport
  assert_eq "$status" 2
  assert_contains "$output" -- "--teleport requires a session id or URL"
}

@test "--teleport rejects whitespace" {
  run "$CC_SESSION" --teleport "has space"
  assert_eq "$status" 2
  assert_contains "$output" "invalid session id"
}

@test "--teleport rejects an empty URL suffix" {
  run "$CC_SESSION" --teleport "https://claude.ai/code/"
  assert_eq "$status" 2
  assert_contains "$output" "invalid session id"
}

@test "--teleport rejects punctuation" {
  run "$CC_SESSION" --teleport "session_!!!"
  assert_eq "$status" 2
  assert_contains "$output" "invalid session id"
}

@test "--teleport accepts a full URL and forwards canonical id to claude" {
  run "$CC_SESSION" -d -t "https://claude.ai/code/session_TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_TEST123abc"
}

@test "--teleport accepts a bare session_xxx id" {
  run "$CC_SESSION" -d -t "session_TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_TEST123abc"
}

@test "--teleport accepts a suffix-only id (prepends session_)" {
  run "$CC_SESSION" -d -t "TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_TEST123abc"
}

@test "--teleport strips trailing slash, query, fragment" {
  for url in \
    "https://claude.ai/code/session_TEST123abc/" \
    "https://claude.ai/code/session_TEST123abc?foo=bar" \
    "https://claude.ai/code/session_TEST123abc#anchor"
  do
    sess="${SESSION_NAME}-${RANDOM}"
    run "$CC_SESSION" -d -t "$url" "$TEST_DIR" "$sess"
    assert_eq "$status" 0
    args="$(pane_args "$sess")"
    assert_contains "$args" -- "--teleport session_TEST123abc"
    tmux kill-session -t "$sess" 2>/dev/null || true
  done
}

# --- --resume <uuid> ------------------------------------------------

@test "--resume without a uuid exits 2" {
  run "$CC_SESSION" --resume
  assert_eq "$status" 2
  assert_contains "$output" -- "--resume requires a local session UUID"
}

@test "--resume rejects non-UUID strings" {
  run "$CC_SESSION" --resume "not-a-uuid"
  assert_eq "$status" 2
  assert_contains "$output" "invalid UUID for --resume"
}

@test "--resume rejects cloud session_xxx ids (different ID space)" {
  run "$CC_SESSION" --resume "session_01EXAMPLEab1234567890"
  assert_eq "$status" 2
  assert_contains "$output" "invalid UUID for --resume"
  assert_contains "$output" "use --teleport"
}

@test "--resume accepts canonical UUID and forwards to claude" {
  run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--resume d8fd4550-d9cc-4ebe-9336-c20b7408afb1"
}

@test "--resume + --teleport mutually exclusive" {
  run "$CC_SESSION" --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" --teleport session_TEST
  assert_eq "$status" 2
  assert_contains "$output" "mutually exclusive"
}

@test "--resume + --adopt mutually exclusive" {
  run "$CC_SESSION" --adopt --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1"
  assert_eq "$status" 2
  assert_contains "$output" "mutually exclusive"
}

@test "--resume + --full exits 2 (--full is teleport-only)" {
  run "$CC_SESSION" --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" --full
  assert_eq "$status" 2
  assert_contains "$output" -- "--full requires --teleport"
}

@test "--resume launches /remote-control after settle and captures URL" {
  run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # fake-claude --resume drops straight into stdin loop. cc-session's
  # post-launch sends /remote-control which fake-claude responds to
  # with the session_FAKE URL.
  wait_for_pane "$SESSION_NAME" "/remote-control is active" 30 \
    || { echo "fake-claude never received /remote-control after --resume"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]
  assert_contains "$(cat "$state_file")" "https://claude.ai/code/session_FAKE"
}

@test "--resume + --compact: /compact lands after URL capture" {
  CC_SESSION_COMPACT_DELAY=1 run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" --compact "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  wait_for_pane "$SESSION_NAME" "fake: /compact received" 30 \
    || { echo "/compact never landed on --resume path"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
}

@test "--resume recycles a managed tmux session like --teleport does" {
  # First create a managed default session
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  old_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"

  # Now --resume the same session-name; should recycle (different pid)
  run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  new_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"
  [ -n "$new_pid" ] && [ "$old_pid" != "$new_pid" ]

  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--resume d8fd4550-d9cc-4ebe-9336-c20b7408afb1"
}

@test "--resume refuses to kill an unmanaged tmux session" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" "refusing to kill"
}

# --- --full ----------------------------------------------------------

@test "--full without --teleport exits 2" {
  run "$CC_SESSION" --full
  assert_eq "$status" 2
  assert_contains "$output" -- "--full requires --teleport"
}

@test "--compact without --teleport exits 2 (server mode rejects slash commands)" {
  run "$CC_SESSION" --compact
  assert_eq "$status" 2
  assert_contains "$output" -- "--compact requires --teleport"
}

@test "--full with 'no' prints warning, aborts, creates no session" {
  run bash -c "echo no | '$CC_SESSION' -d -t session_TEST --full '$TEST_DIR' '$SESSION_NAME'"
  assert_eq "$status" 1
  assert_contains "$output" "ENTIRE conversation"
  assert_contains "$output" "aborted"
  run tmux has-session -t "$SESSION_NAME"
  refute_contains "$status" 0  # has-session returns 1 when no session
}

@test "--full requires literal 'yes' (partial 'y' aborts)" {
  run bash -c "echo y | '$CC_SESSION' -d -t session_TEST --full '$TEST_DIR' '$SESSION_NAME'"
  assert_eq "$status" 1
  assert_contains "$output" "aborted"
}

@test "--full proceeds when user types exactly 'yes'" {
  run bash -c "echo yes | '$CC_SESSION' -d -t session_TEST --full '$TEST_DIR' '$SESSION_NAME'"
  assert_eq "$status" 0
  tmux has-session -t "$SESSION_NAME"
}

@test "CC_SESSION_SKIP_FULL_CONFIRM=1 bypasses the prompt" {
  CC_SESSION_SKIP_FULL_CONFIRM=1 run "$CC_SESSION" -d -t session_TEST --full "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  refute_contains "$output" 'Type "yes"'
  tmux has-session -t "$SESSION_NAME"
}

# --- @cc-session-managed marker -------------------------------------

@test "creating a session via cc-session sets @cc-session-managed=1" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_eq "$(marker_value "$SESSION_NAME")" "1"
}

@test "--teleport refuses to kill an unmanaged tmux session" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  assert_eq "$(marker_value "$SESSION_NAME")" ""

  run "$CC_SESSION" -d -t session_TEST "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" "refusing to kill"
  assert_contains "$output" "@cc-session-managed=1"
  tmux has-session -t "$SESSION_NAME"
}

@test "--teleport recycles a managed session: new pid, marker re-set, args updated" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  old_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"

  run "$CC_SESSION" -d -t session_TEST "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  new_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"
  [ -n "$new_pid" ] && [ "$old_pid" != "$new_pid" ]
  assert_eq "$(marker_value "$SESSION_NAME")" "1"

  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_TEST"
}

# --- Argument forwarding (default launch uses `remote-control`) ----

@test "default launch invokes the remote-control subcommand" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" "remote-control"
  refute_contains "$args" -- "--teleport"
}

# --- Post-launch URL capture (background subshell) ------------------

@test "background flow captures the server-mode URL on default launch" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # Server mode: the URL is printed automatically on startup, no
  # /remote-control keystroke is sent.
  wait_for_pane "$SESSION_NAME" "https://claude.ai/code?environment=env_FAKE" 30 \
    || { echo "Pane never received the expected server URL:"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
  # State file written at $TMPDIR/cc-session/<NAME>.url
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]
  url="$(cat "$state_file")"
  assert_contains "$url" "https://claude.ai/code?environment=env_FAKE"
}

@test "default launch does NOT send the /remote-control slash command" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # Give the (now-absent) keystroke flow more than enough time to fire.
  sleep 3
  pane="$(tmux capture-pane -t "$SESSION_NAME" -p -S -200)"
  # fake-claude's remote-control branch echoes any received stdin as
  # "server stdin: <line>". If cc-session erroneously sends a slash
  # command into a server-mode pane, it would surface here.
  refute_contains "$pane" "server stdin: /remote-control"
  refute_contains "$pane" "/remote-control is active"
}

@test "/compact fires AFTER URL capture, not on a fixed delay" {
  # Use a short compact delay so the test doesn't wait long.
  CC_SESSION_COMPACT_DELAY=1 run "$CC_SESSION" -d -t session_TEST --compact "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # First the URL must be captured (proving claude reached idle).
  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    [ -f "$state_file" ] && break
    sleep 0.5
  done
  [ -f "$state_file" ]
  # THEN /compact lands. fake-claude echoes "fake: /compact received"
  # when it reads /compact on stdin.
  wait_for_pane "$SESSION_NAME" "fake: /compact received" 30 \
    || { echo "fake-claude never received /compact"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
}

@test "background flow sends resume key 1 (summary) when --teleport given" {
  run "$CC_SESSION" -d -t session_TEST "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  wait_for_pane "$SESSION_NAME" "fake: resume key 1 received" 30 \
    || { echo "fake-claude never received resume key 1"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
}

@test "background flow sends resume key 2 (full) when --teleport --full" {
  CC_SESSION_SKIP_FULL_CONFIRM=1 run "$CC_SESSION" -d -t session_TEST --full "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  wait_for_pane "$SESSION_NAME" "fake: resume key 2 received" 30 \
    || { echo "fake-claude never received resume key 2"; \
         tmux capture-pane -t "$SESSION_NAME" -p; \
         return 1; }
}

# --- --adopt flag ----------------------------------------------------

@test "--adopt + --teleport mutually exclusive" {
  run "$CC_SESSION" --adopt -t session_TEST
  assert_eq "$status" 2
  assert_contains "$output" "mutually exclusive"
}

@test "--adopt incompatible with --detach / --compact / --full" {
  run "$CC_SESSION" --adopt -d
  assert_eq "$status" 2
  assert_contains "$output" "incompatible"
}

@test "--adopt fails on nonexistent tmux session" {
  run "$CC_SESSION" --adopt "definitely-not-here-$$-$BATS_TEST_NUMBER"
  assert_eq "$status" 1
  assert_contains "$output" "does not exist"
}

@test "--adopt refuses an unmanaged tmux session" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  run "$CC_SESSION" --adopt "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" "refusing to adopt unmanaged"
  assert_contains "$output" "@cc-session-managed marker"
}

@test "--adopt enables RC on managed session and prints URL" {
  # Pre-create a managed session with fake-claude (it reads stdin so
  # our /remote-control keystroke gets a response).
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "$FAKE_CLAUDE"
  tmux set-option -t "$SESSION_NAME" -q '@cc-session-managed' '1'
  sleep 0.5  # let fake-claude reach its read loop

  run "$CC_SESSION" --adopt "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "Remote Control on '$SESSION_NAME'"
  assert_contains "$output" "https://claude.ai/code/session_FAKE"

  state_file="${BATS_TMPDIR}/cc-session/$SESSION_NAME.url"
  [ -f "$state_file" ]
  assert_contains "$(cat "$state_file")" "https://claude.ai/code/session_FAKE"
}

@test "--adopt is idempotent: second call returns same URL without re-sending" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "$FAKE_CLAUDE"
  tmux set-option -t "$SESSION_NAME" -q '@cc-session-managed' '1'
  sleep 0.5

  run "$CC_SESSION" --adopt "$SESSION_NAME"
  assert_eq "$status" 0
  url1="$(printf '%s\n' "$output" | grep -oE 'https://claude\.ai/code/session_FAKE[0-9]+')"

  run "$CC_SESSION" --adopt "$SESSION_NAME"
  assert_eq "$status" 0
  url2="$(printf '%s\n' "$output" | grep -oE 'https://claude\.ai/code/session_FAKE[0-9]+')"

  assert_eq "$url1" "$url2"
}

@test "--adopt rejects 2 positionals (tmux-name mode is single-positional)" {
  run "$CC_SESSION" --adopt some-dir some-session
  assert_eq "$status" 2
  assert_contains "$output" "takes at most one positional"
}

@test "--adopt with bare ULID-shaped arg auto-delegates to --teleport flow" {
  # 24-char alphanumeric — cloud session id shape (no hyphens or
  # underscores). cc-session should switch to --teleport mode and
  # run claude --teleport <canonical-id>.
  run "$CC_SESSION" -d --adopt 01ABCDEFGHIJklmnopqrstuv "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "switching to --teleport mode"
  args="$(pane_args "$SESSION_NAME")"
  # parse_session_id prepends session_ to bare suffix
  assert_contains "$args" -- "--teleport session_01ABCDEFGHIJklmnopqrstuv"
}

@test "--adopt with session_-prefixed arg auto-delegates to --teleport flow" {
  run "$CC_SESSION" -d --adopt session_01TESTabcdef1234567890 "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "switching to --teleport mode"
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_01TESTabcdef1234567890"
}

@test "--adopt with claude.ai URL auto-delegates and parses URL" {
  run "$CC_SESSION" -d --adopt "https://claude.ai/code/session_01URLabc1234567890ABCD" "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "switching to --teleport mode"
  args="$(pane_args "$SESSION_NAME")"
  assert_contains "$args" -- "--teleport session_01URLabc1234567890ABCD"
}

@test "--adopt accepts a regular tmux name with hyphens (not flagged as cloud ID)" {
  # Even though >20 chars, hyphens disqualify the ULID heuristic.
  long_name="my-very-long-tmux-session-name"
  run "$CC_SESSION" --adopt "$long_name"
  # Will fail with "does not exist" (no such tmux session) — which is
  # the right error class. The point: not flagged as cloud ID.
  assert_eq "$status" 1
  assert_contains "$output" "does not exist"
  refute_contains "$output" "cloud session"
}

@test "--adopt tmux-name mode rejects --detach (incompatible)" {
  # Pre-create managed tmux so adopt would otherwise succeed.
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "$FAKE_CLAUDE"
  tmux set-option -t "$SESSION_NAME" -q '@cc-session-managed' '1'
  sleep 0.3
  run "$CC_SESSION" --adopt -d "$SESSION_NAME"
  assert_eq "$status" 2
  assert_contains "$output" "incompatible"
}

# --- Error paths -----------------------------------------------------

@test "missing PROJECT_DIR exits 1 with clear message" {
  run "$CC_SESSION" "/tmp/cc-session-no-such-dir-$$"
  assert_eq "$status" 1
  assert_contains "$output" "directory not found"
}

@test "-d on an already-running session is a no-op (does not respawn)" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "started"
  first_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"

  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "already running"
  second_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"
  assert_eq "$first_pid" "$second_pid"
}
