#!/usr/bin/env bats
#
# Black-box tests for cc-session. The script is zsh and bats runs in
# bash, so we never source it — every test invokes cc-session as a
# subprocess. claude is stubbed with tests/fixtures/fake-claude so we
# never touch the real CLI, the cloud, or anything network-bound.

setup() {
  CC_SESSION="${BATS_TEST_DIRNAME}/../cc-session"
  FAKE_CLAUDE="${BATS_TEST_DIRNAME}/fixtures/fake-claude"
  chmod +x "$FAKE_CLAUDE" "$CC_SESSION"

  TEST_DIR="${BATS_TMPDIR}/cc-session-test-$$-${BATS_TEST_NUMBER}"
  mkdir -p "$TEST_DIR"

  SESSION_NAME="cc-test-$$-${BATS_TEST_NUMBER}"

  export CLAUDE_BIN="$FAKE_CLAUDE"
  # Ensure tmux uses an isolated socket per test run so we don't collide
  # with any tmux server the user / CI runner already has.
  export TMUX_TMPDIR="${BATS_TMPDIR}/cc-session-tmux-$$"
  mkdir -p "$TMUX_TMPDIR"
}

teardown() {
  tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
  rm -rf "$TEST_DIR"
}

# --- Helpers ----------------------------------------------------------

# Read the "fake claude args: ..." line from a session's first pane.
pane_args() {
  local sess="$1"
  # Give fake-claude a moment to print
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    out="$(tmux capture-pane -t "$sess" -p 2>/dev/null | grep '^fake claude args:' || true)"
    [ -n "$out" ] && { printf '%s\n' "$out"; return 0; }
    sleep 0.1
  done
  return 1
}

marker_value() {
  tmux show-options -t "$1" -v '@cc-session-managed' 2>/dev/null || true
}

# --- usage / help ----------------------------------------------------

@test "--help renders and exits 0 with all sections" {
  run "$CC_SESSION" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *NAME* ]]
  [[ "$output" == *SYNOPSIS* ]]
  [[ "$output" == *--teleport* ]]
  [[ "$output" == *--full* ]]
  [[ "$output" == *--compact* ]]
  [[ "$output" == *@cc-session-managed* ]]
  [[ "$output" == *CC_SESSION_SKIP_FULL_CONFIRM* ]]
  [[ "$output" == *CC_SESSION_COMPACT_DELAY* ]]
}

@test "-h is an alias for --help" {
  run "$CC_SESSION" -h
  [ "$status" -eq 0 ]
  [[ "$output" == *NAME* ]]
}

@test "unknown flag exits 2 with hint" {
  run "$CC_SESSION" --bogus
  [ "$status" -eq 2 ]
  [[ "$output" == *"unknown option: --bogus"* ]]
  [[ "$output" == *"Try"* ]]
}

# --- --kill / --list scaffolding ------------------------------------

@test "--kill without a name exits 2" {
  run "$CC_SESSION" --kill
  [ "$status" -eq 2 ]
  [[ "$output" == *"--kill requires a session name"* ]]
}

# --- parse_session_id (exercised via --teleport) --------------------

@test "--teleport without an id exits 2" {
  run "$CC_SESSION" --teleport
  [ "$status" -eq 2 ]
  [[ "$output" == *"--teleport requires a session id or URL"* ]]
}

@test "--teleport rejects whitespace" {
  run "$CC_SESSION" --teleport "has space"
  [ "$status" -eq 2 ]
  [[ "$output" == *"invalid session id"* ]]
}

@test "--teleport rejects an empty URL suffix" {
  run "$CC_SESSION" --teleport "https://claude.ai/code/"
  [ "$status" -eq 2 ]
  [[ "$output" == *"invalid session id"* ]]
}

@test "--teleport rejects punctuation" {
  run "$CC_SESSION" --teleport "session_!!!"
  [ "$status" -eq 2 ]
  [[ "$output" == *"invalid session id"* ]]
}

@test "--teleport accepts a full URL and forwards canonical id to claude" {
  run "$CC_SESSION" -d -t "https://claude.ai/code/session_TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--remote-control"* ]]
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

@test "--teleport accepts a bare session_xxx id" {
  run "$CC_SESSION" -d -t "session_TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

@test "--teleport accepts a suffix-only id (prepends session_)" {
  run "$CC_SESSION" -d -t "TEST123abc" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

@test "--teleport strips a trailing slash from a URL" {
  run "$CC_SESSION" -d -t "https://claude.ai/code/session_TEST123abc/" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

@test "--teleport strips a query string" {
  run "$CC_SESSION" -d -t "https://claude.ai/code/session_TEST123abc?foo=bar" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

@test "--teleport strips a fragment" {
  run "$CC_SESSION" -d -t "https://claude.ai/code/session_TEST123abc#anchor" "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

# --- --full -----------------------------------------------------------

@test "--full without --teleport exits 2" {
  run "$CC_SESSION" --full
  [ "$status" -eq 2 ]
  [[ "$output" == *"--full requires --teleport"* ]]
}

@test "--full with 'no' prints warning, aborts, creates no session" {
  run bash -c "echo no | '$CC_SESSION' -d -t session_TEST123abc --full '$TEST_DIR' '$SESSION_NAME'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"--full will resume the ENTIRE conversation"* ]]
  [[ "$output" == *"aborted"* ]]
  run tmux has-session -t "$SESSION_NAME"
  [ "$status" -ne 0 ]
}

@test "--full requires literal 'yes' (a partial 'y' aborts)" {
  run bash -c "echo y | '$CC_SESSION' -d -t session_TEST123abc --full '$TEST_DIR' '$SESSION_NAME'"
  [ "$status" -eq 1 ]
  [[ "$output" == *"aborted"* ]]
}

@test "--full proceeds when user types exactly 'yes'" {
  run bash -c "echo yes | '$CC_SESSION' -d -t session_TEST123abc --full '$TEST_DIR' '$SESSION_NAME'"
  [ "$status" -eq 0 ]
  tmux has-session -t "$SESSION_NAME"
}

@test "CC_SESSION_SKIP_FULL_CONFIRM=1 bypasses the prompt" {
  CC_SESSION_SKIP_FULL_CONFIRM=1 run "$CC_SESSION" -d -t session_TEST123abc --full "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  [[ "$output" != *'Type "yes"'* ]]
  tmux has-session -t "$SESSION_NAME"
}

# --- @cc-session-managed marker --------------------------------------

@test "creating a session via cc-session sets @cc-session-managed=1" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  [ "$(marker_value "$SESSION_NAME")" = "1" ]
}

@test "--teleport refuses to kill an unmanaged tmux session" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  [ -z "$(marker_value "$SESSION_NAME")" ]

  run "$CC_SESSION" -d -t session_TEST123abc "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 1 ]
  [[ "$output" == *"refusing to kill"* ]]
  [[ "$output" == *"@cc-session-managed=1"* ]]

  tmux has-session -t "$SESSION_NAME"
}

@test "--teleport recycles a managed session: new pid, marker re-set, args updated" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  old_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"

  run "$CC_SESSION" -d -t session_TEST123abc "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]

  new_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"
  [ -n "$new_pid" ]
  [ "$old_pid" != "$new_pid" ]

  [ "$(marker_value "$SESSION_NAME")" = "1" ]

  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--teleport session_TEST123abc"* ]]
}

# --- Default launch / argument forwarding ----------------------------

@test "default launch forwards --remote-control (no --teleport)" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  args="$(pane_args "$SESSION_NAME")"
  [[ "$args" == *"--remote-control"* ]]
  [[ "$args" != *"--teleport"* ]]
}

# --- Error paths -----------------------------------------------------

@test "missing PROJECT_DIR exits 1 with clear message" {
  run "$CC_SESSION" "/tmp/cc-session-no-such-dir-$$"
  [ "$status" -eq 1 ]
  [[ "$output" == *"directory not found"* ]]
}

@test "-d on an already-running session is a no-op (does not respawn)" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  [[ "$output" == *"started"* ]]
  first_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"

  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  [ "$status" -eq 0 ]
  [[ "$output" == *"already running"* ]]
  second_pid="$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}' | head -1)"
  [ "$first_pid" = "$second_pid" ]
}
