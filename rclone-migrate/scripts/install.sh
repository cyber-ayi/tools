#!/usr/bin/env bash
# Install rclone-migrate from a signed GitHub Release artifact.
#
# Usage (one-liner):
#   curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/install.sh | bash
#
# Audited form (recommended):
#   curl -fsSL https://raw.githubusercontent.com/Jarvie8176/tools/main/rclone-migrate/scripts/install.sh -o install.sh
#   less install.sh
#   bash install.sh
#
# Flags:
#   --version X.Y.Z   Pin to an exact version (default: latest stable release)
#   --prerelease      Allow pre-release tags when picking latest
#   --help, -h        Show usage

set -euo pipefail

REPO="${RCLONE_MIGRATE_REPO:-Jarvie8176/tools}"
TAG_PREFIX="${RCLONE_MIGRATE_TAG_PREFIX:-rclone-migrate-v}"
PKG_NAME="rclone-migrate"
WHEEL_DIST="rclone_migrate"  # PEP 503 normalized distribution name

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    RED=$(printf '\033[31m')
    RESET=$(printf '\033[0m')
else
    GREEN=""
    YELLOW=""
    RED=""
    RESET=""
fi
log()  { printf '%s==>%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%swarn:%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
err()  { printf '%serror:%s %s\n' "$RED" "$RESET" "$*" >&2; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
VERSION=""
PRERELEASE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --version)    VERSION="$2"; shift 2 ;;
        --version=*)  VERSION="${1#*=}"; shift ;;
        --prerelease) PRERELEASE=1; shift ;;
        --help|-h)
            sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) die "Unknown argument: $1 (try --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Platform check
# ---------------------------------------------------------------------------
case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "Unsupported platform: $(uname -s). This installer supports Linux and macOS only.
       On Windows, install directly: pipx install $PKG_NAME (after a manual wheel download)." ;;
esac

# ---------------------------------------------------------------------------
# Python check
# ---------------------------------------------------------------------------
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
            PY="$cand"
            break
        fi
    fi
done
[ -n "$PY" ] || die "Need Python 3.9+ on PATH. Install Python (e.g. via Homebrew, apt, pyenv) and re-run."
log "Using Python: $("$PY" --version 2>&1) ($("$PY" -c 'import sys; print(sys.executable)'))"

# ---------------------------------------------------------------------------
# pipx check (install user-level if missing)
# ---------------------------------------------------------------------------
if ! command -v pipx >/dev/null 2>&1; then
    log "pipx not found; installing user-level..."
    "$PY" -m pip install --user --quiet --upgrade pipx
    "$PY" -m pipx ensurepath
    USER_BIN=$("$PY" -c 'import site, os; print(os.path.join(site.USER_BASE, "bin"))')
    export PATH="$USER_BIN:$PATH"
fi

# ---------------------------------------------------------------------------
# rclone presence (warn-only — rmig needs it at runtime, not at install)
# ---------------------------------------------------------------------------
if ! command -v rclone >/dev/null 2>&1; then
    warn "rclone is not on PATH. rmig depends on it at runtime."
    warn "Install: https://rclone.org/downloads/"
fi

# ---------------------------------------------------------------------------
# Resolve target version
# ---------------------------------------------------------------------------
if [ -z "$VERSION" ]; then
    log "Querying latest release (prefix: ${TAG_PREFIX})..."
    VERSION=$("$PY" - "$REPO" "$TAG_PREFIX" "$PRERELEASE" <<'PYEOF'
import json
import sys
import urllib.error
import urllib.request

repo, prefix, allow_pre = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        releases = json.load(r)
except urllib.error.URLError as e:
    print(f"Failed to query GitHub: {e}", file=sys.stderr)
    sys.exit(2)

for rel in releases:
    if rel.get("draft"):
        continue
    if rel.get("prerelease") and not allow_pre:
        continue
    tag = rel.get("tag_name", "")
    if tag.startswith(prefix):
        print(tag[len(prefix):])
        sys.exit(0)

print("ERR: no matching release found", file=sys.stderr)
sys.exit(1)
PYEOF
)
    [ -n "$VERSION" ] || die "Could not resolve a release version. Try --version X.Y.Z or check https://github.com/$REPO/releases"
fi
TAG="${TAG_PREFIX}${VERSION}"
log "Installing $PKG_NAME $VERSION (tag: $TAG)"

# ---------------------------------------------------------------------------
# Download wheel + SHA256SUMS
# ---------------------------------------------------------------------------
TMPDIR=$(mktemp -d 2>/dev/null || mktemp -d -t 'rmig-install')
trap 'rm -rf "$TMPDIR"' EXIT

WHEEL_FILE="${WHEEL_DIST}-${VERSION}-py3-none-any.whl"
BASE_URL="https://github.com/${REPO}/releases/download/${TAG}"

log "Downloading $WHEEL_FILE..."
curl -fsSL "${BASE_URL}/${WHEEL_FILE}" -o "${TMPDIR}/${WHEEL_FILE}" \
    || die "Failed to download wheel from ${BASE_URL}/${WHEEL_FILE}"

log "Downloading SHA256SUMS..."
curl -fsSL "${BASE_URL}/SHA256SUMS" -o "${TMPDIR}/SHA256SUMS" \
    || die "Failed to download SHA256SUMS from ${BASE_URL}/SHA256SUMS"

# ---------------------------------------------------------------------------
# Verify SHA-256
# ---------------------------------------------------------------------------
log "Verifying checksum..."
if command -v sha256sum >/dev/null 2>&1; then
    SHA_CMD=(sha256sum)
elif command -v shasum >/dev/null 2>&1; then
    SHA_CMD=(shasum -a 256)
else
    die "Need 'sha256sum' or 'shasum' on PATH for verification."
fi

EXPECTED=$(awk -v f="$WHEEL_FILE" '$2 == f || $2 == "*"f {print $1; exit}' "${TMPDIR}/SHA256SUMS")
[ -n "$EXPECTED" ] || die "No checksum entry for $WHEEL_FILE in SHA256SUMS."
ACTUAL=$( ( cd "$TMPDIR" && "${SHA_CMD[@]}" "$WHEEL_FILE" ) | awk '{print $1}')
[ "$ACTUAL" = "$EXPECTED" ] || die "Checksum mismatch for $WHEEL_FILE
  expected: $EXPECTED
  actual:   $ACTUAL"
log "Checksum verified."

# ---------------------------------------------------------------------------
# Install via pipx (force = clean reinstall on upgrade)
# ---------------------------------------------------------------------------
log "Installing with pipx..."
pipx install --force "${TMPDIR}/${WHEEL_FILE}" >/dev/null

# ---------------------------------------------------------------------------
# Verify install
# ---------------------------------------------------------------------------
if command -v rmig >/dev/null 2>&1; then
    log "Installed: $(rmig --version 2>/dev/null || echo "rmig (unknown version)")"
    log "Try: rmig --help"
else
    warn "Install completed but 'rmig' is not on PATH yet."
    warn "Run: $PY -m pipx ensurepath"
    warn "Then start a new shell."
fi
