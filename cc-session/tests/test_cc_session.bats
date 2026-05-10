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
  assert_contains "$output" -- --full
  assert_contains "$output" -- --compact
  assert_contains "$output" -- --adopt
  assert_contains "$output" "@cc-session-managed"
  assert_contains "$output" CC_SESSION_SKIP_FULL_CONFIRM
  assert_contains "$output" CC_SESSION_COMPACT_DELAY
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

# --- --full ----------------------------------------------------------

@test "--full without --teleport exits 2" {
  run "$CC_SESSION" --full
  assert_eq "$status" 2
  assert_contains "$output" -- "--full requires --teleport"
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

# --- Argument forwarding (default launch passes NO startup flags) ---

@test "default launch passes no startup flags (RC enabled via slash command later)" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  args="$(pane_args "$SESSION_NAME")"
  refute_contains "$args" -- "--remote-control"
  refute_contains "$args" -- "--teleport"
}

# --- Post-launch RC enable (background subshell) --------------------

@test "background flow sends /remote-control and captures URL on default launch" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  # Wait for the background subshell to send /remote-control and for
  # fake-claude to print the synthetic active line into the pane.
  wait_for_pane "$SESSION_NAME" "/remote-control is active" 30 \
    || { echo "Pane never received the expected RC active line:"; \
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
  assert_contains "$url" "https://claude.ai/code/session_FAKE"
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
  run "$CC_SESSION" --adopt "$TEST_DIR" "definitely-not-here-$$-$BATS_TEST_NUMBER"
  assert_eq "$status" 1
  assert_contains "$output" "does not exist"
}

@test "--adopt refuses an unmanaged tmux session" {
  tmux new-session -d -s "$SESSION_NAME" -c "$TEST_DIR" "sleep 3600"
  run "$CC_SESSION" --adopt "$TEST_DIR" "$SESSION_NAME"
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

  run "$CC_SESSION" --adopt "$TEST_DIR" "$SESSION_NAME"
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

  run "$CC_SESSION" --adopt "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  url1="$(printf '%s\n' "$output" | grep -oE 'https://claude\.ai/code/session_FAKE[0-9]+')"

  run "$CC_SESSION" --adopt "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  url2="$(printf '%s\n' "$output" | grep -oE 'https://claude\.ai/code/session_FAKE[0-9]+')"

  assert_eq "$url1" "$url2"
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
