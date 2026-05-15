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
  assert_contains "$output" CC_SESSION_RESUME_TIMEOUT
  assert_contains "$output" CC_SESSION_RC_URL_TIMEOUT
  assert_contains "$output" CC_SESSION_RC_ENABLE_TIMEOUT
  assert_contains "$output" -- --update
  assert_contains "$output" CC_SESSION_UPDATE_URL
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

@test "remain-on-exit preserves crashed pane buffer and --status reports alive=no" {
  # Launch with a claude stub that exits immediately. Without
  # remain-on-exit the pane would be destroyed and its scrollback wiped
  # — making the crash undebuggable. With the option the pane stays
  # with pane_dead=1, scrollback intact, and --status flips alive→no.
  CC_FAKE_CLAUDE_CRASH=1 \
    CC_SESSION_RC_URL_TIMEOUT=2 \
    run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0

  # Wait for the stub to exit and the background URL-poll to notice.
  # The early-exit branch should fire within ~0.5s once pane_dead=1.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    pd="$(tmux list-panes -s -t "$SESSION_NAME" -F '#{pane_dead}' 2>/dev/null | head -1 || true)"
    [ "$pd" = "1" ] && break
    sleep 0.3
  done
  assert_eq "$pd" "1"

  # Crash output (stdout AND stderr) must still be capturable.
  buf="$(tmux capture-pane -t "$SESSION_NAME" -p -S -200 2>/dev/null)"
  assert_contains "$buf" "FAKE CLAUDE: crashing on purpose"
  assert_contains "$buf" "FAKE CLAUDE: stderr line"

  # --status must report alive=no even though tmux has-session=true.
  run "$CC_SESSION" --status "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" "alive: no"
  assert_contains "$output" "managed: yes"
}

@test "remain-on-exit is set as a window option on newly created sessions" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  opt="$(tmux show-window-options -t "$SESSION_NAME" -v remain-on-exit 2>/dev/null || true)"
  assert_eq "$opt" "on"
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

# --- --worktree -----------------------------------------------------

@test "--worktree without a name exits 2" {
  run "$CC_SESSION" --worktree
  assert_eq "$status" 2
  assert_contains "$output" -- "--worktree requires a name"
}

@test "--worktree refuses 'main' (and other protected refs)" {
  for name in main master origin/main origin/master; do
    run "$CC_SESSION" --worktree "$name" "$TEST_DIR" "$SESSION_NAME"
    assert_eq "$status" 2
    assert_contains "$output" "refusing to create worktree on '$name'"
  done
}

@test "--worktree on a non-git PROJECT_DIR exits 1" {
  run "$CC_SESSION" --worktree foo "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 1
  assert_contains "$output" -- "--worktree requires PROJECT_DIR to be a git repo"
}

@test "--worktree on a real git repo creates branch + path + launches claude" {
  # Build a tiny throwaway repo with an 'origin/main' to base off.
  upstream="${TEST_DIR}/upstream.git"
  workrepo="${TEST_DIR}/work"
  git init --bare -q "$upstream"
  git init -q -b main "$workrepo"
  git -C "$workrepo" remote add origin "$upstream"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init
  git -C "$workrepo" push -q origin main

  run "$CC_SESSION" -d -w ops/foo "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "worktree ready"

  wt="${TEST_DIR}/work-wt/foo"
  [ -d "$wt" ] || { echo "expected worktree dir: $wt"; return 1; }
  branch="$(git -C "$wt" rev-parse --abbrev-ref HEAD)"
  assert_eq "$branch" "ops/foo"

  # Cleanup: tmux teardown is handled by the per-test teardown.
}

@test "--worktree refuses a path that already exists" {
  upstream="${TEST_DIR}/upstream.git"
  workrepo="${TEST_DIR}/work"
  git init --bare -q "$upstream"
  git init -q -b main "$workrepo"
  git -C "$workrepo" remote add origin "$upstream"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init
  git -C "$workrepo" push -q origin main

  # First call succeeds.
  run "$CC_SESSION" -d -w ops/foo "$workrepo" "${SESSION_NAME}-a"
  assert_eq "$status" 0

  # Second call with same NAME must error before touching anything.
  run "$CC_SESSION" -d -w ops/foo "$workrepo" "${SESSION_NAME}-b"
  assert_eq "$status" 1
  assert_contains "$output" "worktree path already exists"
  assert_contains "$output" "git worktree remove"
}

@test "hint fires when PROJECT_DIR is a git repo on a non-main branch" {
  workrepo="${TEST_DIR}/work"
  git init -q -b main "$workrepo"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init
  git -C "$workrepo" checkout -q -b ops/leftover

  run "$CC_SESSION" -d "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0
  assert_contains "$output" "hint"
  assert_contains "$output" "branch 'ops/leftover'"
  assert_contains "$output" "-w ops/<task>"
  assert_contains "$output" "checkout main"
}

@test "hint is silent when PROJECT_DIR is on main" {
  workrepo="${TEST_DIR}/work"
  git init -q -b main "$workrepo"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init

  run "$CC_SESSION" -d "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0
  refute_contains "$output" "hint"
}

@test "hint is suppressed by CC_SESSION_NO_WORKTREE_HINT=1" {
  workrepo="${TEST_DIR}/work"
  git init -q -b main "$workrepo"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init
  git -C "$workrepo" checkout -q -b ops/leftover

  CC_SESSION_NO_WORKTREE_HINT=1 \
    run "$CC_SESSION" -d "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0
  refute_contains "$output" "hint"
}

@test "hint is silent when --worktree is already in use" {
  upstream="${TEST_DIR}/upstream.git"
  workrepo="${TEST_DIR}/work"
  git init --bare -q "$upstream"
  git init -q -b main "$workrepo"
  git -C "$workrepo" remote add origin "$upstream"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m init
  git -C "$workrepo" push -q origin main
  git -C "$workrepo" checkout -q -b ops/leftover

  run "$CC_SESSION" -d -w ops/new "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0
  refute_contains "$output" "hint"
}

@test "hint is silent when PROJECT_DIR is not a git repo" {
  run "$CC_SESSION" -d "$TEST_DIR" "$SESSION_NAME"
  assert_eq "$status" 0
  refute_contains "$output" "hint"
}

@test "CC_SESSION_WORKTREE_BASE overrides the default 'origin/main' base ref" {
  upstream="${TEST_DIR}/upstream.git"
  workrepo="${TEST_DIR}/work"
  git init --bare -q "$upstream"
  git init -q -b main "$workrepo"
  git -C "$workrepo" remote add origin "$upstream"
  git -C "$workrepo" config user.email t@t
  git -C "$workrepo" config user.name t
  git -C "$workrepo" commit --allow-empty -q -m main-base
  git -C "$workrepo" push -q origin main
  # Build an alternate branch with a distinct commit, push it.
  git -C "$workrepo" checkout -q -b alt
  git -C "$workrepo" commit --allow-empty -q -m alt-base
  alt_sha="$(git -C "$workrepo" rev-parse HEAD)"
  git -C "$workrepo" push -q origin alt
  git -C "$workrepo" checkout -q main

  CC_SESSION_WORKTREE_BASE=origin/alt \
    run "$CC_SESSION" -d -w ops/from-alt "$workrepo" "$SESSION_NAME"
  assert_eq "$status" 0

  wt="${TEST_DIR}/work-wt/from-alt"
  wt_sha="$(git -C "$wt" rev-parse HEAD)"
  # New branch must point at origin/alt's tip, not main's.
  assert_eq "$wt_sha" "$alt_sha"
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
  run "$CC_SESSION" -d --resume "d8fd4550-d9cc-4ebe-9336-c20b7408afb1" --compact "$TEST_DIR" "$SESSION_NAME"
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

@test "--full + --compact rejected as self-defeating" {
  # Self-defeating: --full pays for full transcript load, --compact
  # immediately summarizes it. Strictly worse than just --teleport
  # (default summary mode).
  CC_SESSION_SKIP_FULL_CONFIRM=1 run "$CC_SESSION" -t session_TEST --full --compact
  assert_eq "$status" 2
  assert_contains "$output" "don't combine sensibly"
  assert_contains "$output" "drop --full"
  assert_contains "$output" "drop --compact"
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
  run "$CC_SESSION" -d -t session_TEST --compact "$TEST_DIR" "$SESSION_NAME"
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

# --- --update --------------------------------------------------------

# Build a throwaway git repo at $TEST_DIR/$1 with N empty commits.
mk_repo() {
  local path="$TEST_DIR/$1"; shift
  local n="${1:-1}"
  git init -q -b main "$path"
  git -C "$path" config user.email "t@t"
  git -C "$path" config user.name "t"
  local i
  for i in $(seq 1 "$n"); do
    git -C "$path" commit --allow-empty -q -m "c$i"
  done
}

@test "--update without git repo errors with install hint" {
  CC_SESSION_UPDATE_REPO="$TEST_DIR" run "$CC_SESSION" --update
  assert_eq "$status" 1
  assert_contains "$output" "requires a git checkout"
  assert_contains "$output" "git clone"
}

@test "--update --check on synced repo reports up to date" {
  mk_repo synced 1
  CC_SESSION_UPDATE_REPO="$TEST_DIR/synced" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/synced" \
    run "$CC_SESSION" --update --check
  assert_eq "$status" 0
  assert_contains "$output" "up to date"
}

@test "--update --check on behind repo lists upstream commits" {
  mk_repo upstream 1
  # Clone the upstream into 'local' before adding c2, so 'local' is one
  # commit behind once upstream gets its second commit.
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2 new upstream"

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    run "$CC_SESSION" --update --check
  assert_eq "$status" 0
  assert_contains "$output" "1 upstream commit"
}

@test "--update on behind repo refuses without tty + UPDATE_YES" {
  mk_repo upstream 1
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2 new upstream"

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    run "$CC_SESSION" --update
  assert_eq "$status" 1
  assert_contains "$output" "stdin is not a tty"
}

@test "--update with UPDATE_YES fast-forwards a behind repo" {
  mk_repo upstream 1
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2 new upstream"

  local before_sha after_sha upstream_sha
  before_sha=$(git -C "$TEST_DIR/local" rev-parse HEAD)
  upstream_sha=$(git -C "$TEST_DIR/upstream" rev-parse HEAD)

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    CC_SESSION_UPDATE_YES=1 \
    run "$CC_SESSION" --update
  assert_eq "$status" 0
  after_sha=$(git -C "$TEST_DIR/local" rev-parse HEAD)
  assert_eq "$after_sha" "$upstream_sha"
  [[ "$after_sha" != "$before_sha" ]]
}

@test "--update refuses when local has diverged commits" {
  mk_repo upstream 1
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  # Upstream advances by one commit; local independently advances by
  # one commit. Both branches are now 1-ahead-of-the-other.
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2-upstream"
  git -C "$TEST_DIR/local" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2-local"

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    CC_SESSION_UPDATE_YES=1 \
    run "$CC_SESSION" --update
  assert_eq "$status" 1
  assert_contains "$output" "refusing to update"
  assert_contains "$output" "not in upstream"
}

@test "--update refuses when working tree is dirty" {
  mk_repo upstream 1
  # Commit a tracked file in upstream so the clone has something to dirty.
  echo "v1" > "$TEST_DIR/upstream/file"
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t add file
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit -q -m "add tracked file"
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  # Upstream advances; local stays behind so the dirty check is reached.
  git -C "$TEST_DIR/upstream" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c3 new upstream"
  # Modify the tracked file in local without staging/committing.
  echo "v2-dirty" > "$TEST_DIR/local/file"

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    CC_SESSION_UPDATE_YES=1 \
    run "$CC_SESSION" --update
  assert_eq "$status" 1
  assert_contains "$output" "uncommitted changes"
}

@test "--update on local-ahead repo reports nothing to pull" {
  mk_repo upstream 1
  git clone -q "$TEST_DIR/upstream" "$TEST_DIR/local"
  git -C "$TEST_DIR/local" -c user.email=t@t -c user.name=t \
    commit --allow-empty -q -m "c2 local-only"

  CC_SESSION_UPDATE_REPO="$TEST_DIR/local" \
    CC_SESSION_UPDATE_URL="$TEST_DIR/upstream" \
    run "$CC_SESSION" --update
  assert_eq "$status" 0
  assert_contains "$output" "ahead of upstream"
}
