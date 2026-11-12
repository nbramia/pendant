#!/bin/zsh
# Runs the screen tour (ScreenTourUITests) and exports its screenshots as PNGs.
#
#   ios/scripts/screens.sh [--device] [output-dir]   default dir: ios/design/screens/<today>
#
# Default target: the iOS Simulator (PENDANT_SIMULATOR, default "iPhone 18 Pro", the same
# screen size as the wearer's phone). Use it for every check and iteration; it never touches
# the phone.
#
# --device runs the tour on the wearer's phone. That takes over his screen, so it is for the
# single pass right before merging a significant UI PR, never for iterating:
#   - only one device run at a time (a lock in /tmp; a lock whose owner died is reclaimed);
#   - one device run per branch: a second on the same branch is refused unless
#     PENDANT_DEVICE_RERUN_REASON says why (logged);
#   - a hard timeout (PENDANT_DEVICE_TIMEOUT seconds, default 900), and no retries: if it
#     fails or hangs, it stops; fix the cause on the simulator, don't rerun it in a loop.
# Every device run is logged to ~/Library/Logs/pendant-device-tours.log.
#
# Run it on the Mac from the GUI session (Terminal), not over SSH: codesigning for the phone
# needs the unlocked login keychain. PENDANT_DEVICE picks the phone (its UDID).
# PENDANT_DERIVED_DATA sets the build folder (default /tmp/pd-ui-sim or /tmp/pd-ui).
# A device run installs a debug build and closes the app, so it relaunches it at the end,
# also after a timeout or an interrupt (the pendant only buffers while the app is closed).
set -u
export PATH=/opt/homebrew/bin:$PATH
IOS="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
MODE=simulator
if [[ "${1:-}" == "--device" ]]; then MODE=device; shift; fi
OUT="${1:-$IOS/design/screens/$(date +%F)}"
BRANCH="$(git -C "$IOS" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
SHA="$(git -C "$IOS" rev-parse --short HEAD 2>/dev/null || echo unknown)"
LEDGER="$HOME/Library/Logs/pendant-device-tours.log"
BUNDLE_ID="$(sed -n 's/^PENDANT_BUNDLE_ID *= *//p' "$IOS/Secrets.xcconfig" 2>/dev/null | head -1)"
BUNDLE_ID="${BUNDLE_ID:-com.example.pendant}"
LOCK=/tmp/pendant-device-tour.lock
XB=""
WATCH=""
DEVICE=""

relaunch_app() {
  for i in {1..30}; do
    xcrun devicectl device process launch --terminate-existing --device "$DEVICE" "$BUNDLE_ID" \
      >/dev/null 2>&1 && return 0
    sleep 10
  done
  echo "WARNING: could not relaunch the app on the phone; open it by hand" >&2
  return 1
}

stop_test() {
  # Stop xcodebuild and anything it started: TERM, then KILL if it lingers.
  [[ -n "$XB" ]] || return 0
  pkill -TERM -P "$XB" 2>/dev/null; kill -TERM "$XB" 2>/dev/null
  for i in {1..10}; do kill -0 "$XB" 2>/dev/null || return 0; sleep 1; done
  pkill -KILL -P "$XB" 2>/dev/null; kill -KILL "$XB" 2>/dev/null
}

cleanup_device() {
  [[ -n "$WATCH" ]] && kill "$WATCH" 2>/dev/null
  stop_test
  relaunch_app
  rm -rf "$LOCK"
}

if [[ "$MODE" == device ]]; then
  # Physical devices only: xctrace lists them before the "== Simulators ==" section.
  DEVICE="${PENDANT_DEVICE:-$(xcrun xctrace list devices 2>/dev/null | sed '/== Simulators ==/,$d' \
    | grep -m1 iPhone | grep -oE '\(([0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f-]{25,})\)$' | tr -d '()')}"
  [[ -n "$DEVICE" ]] || { echo "no phone found; set PENDANT_DEVICE" >&2; exit 1; }
  if ! mkdir "$LOCK" 2>/dev/null; then
    OWNER_PID="$(cat "$LOCK/pid" 2>/dev/null)"
    if [[ -n "$OWNER_PID" ]] && kill -0 "$OWNER_PID" 2>/dev/null; then
      echo "another device tour is running ($(cat "$LOCK/owner" 2>/dev/null)); not starting a second" >&2
      exit 3
    fi
    echo "reclaiming a stale device-tour lock (owner gone)" >&2
    rm -rf "$LOCK"; mkdir "$LOCK" || { echo "could not take the device-tour lock" >&2; exit 3; }
  fi
  echo $$ > "$LOCK/pid"
  echo "$BRANCH $SHA $(date '+%F %T')" > "$LOCK/owner"
  trap 'rm -rf "$LOCK"' EXIT
  trap 'cleanup_device; exit 130' INT TERM HUP
  mkdir -p "$(dirname "$LEDGER")" || { echo "cannot create $(dirname "$LEDGER")" >&2; exit 5; }
  # Exact match on the branch field (no regex), so similar branch names don't collide.
  if awk -v b="branch=$BRANCH" '{ for (i = 1; i <= NF; i++) if ($i == b) found = 1 } END { exit !found }' \
       "$LEDGER" 2>/dev/null && [[ -z "${PENDANT_DEVICE_RERUN_REASON:-}" ]]; then
    echo "branch $BRANCH already had its on-device pass; iterate on the simulator." >&2
    echo "If a second device run is truly needed, set PENDANT_DEVICE_RERUN_REASON." >&2
    exit 4
  fi
  REASON="${PENDANT_DEVICE_RERUN_REASON:-first-pass}"
  echo "$(date '+%F %T') branch=$BRANCH sha=$SHA reason=${REASON// /_}" >> "$LEDGER" \
    || { echo "cannot write $LEDGER; not running on the phone" >&2; exit 5; }
  DEST="id=$DEVICE"
  DD="${PENDANT_DERIVED_DATA:-/tmp/pd-ui}"
else
  SIM="${PENDANT_SIMULATOR:-iPhone 18 Pro}"
  DEST="platform=iOS Simulator,name=$SIM"
  DD="${PENDANT_DERIVED_DATA:-/tmp/pd-ui-sim}"
fi

RESULT="$(mktemp -d)/tour.xcresult"
cd "$IOS" || exit 1
xcodegen -q || { echo "xcodegen failed" >&2; exit 1; }
xcodebuild test -project PendantDirect.xcodeproj -scheme PendantDirect -destination "$DEST" \
  -derivedDataPath "$DD" -allowProvisioningUpdates -resultBundlePath "$RESULT" \
  -collect-test-diagnostics never \
  -only-testing:PendantDirectUITests/ScreenTourUITests &
XB=$!
if [[ "$MODE" == device ]]; then
  # Hard stop: a hung device run must not hold the phone.
  ( sleep "${PENDANT_DEVICE_TIMEOUT:-900}"; echo "device tour timed out; stopping it" >&2
    pkill -TERM -P "$XB" 2>/dev/null; kill -TERM "$XB" 2>/dev/null; sleep 10
    pkill -KILL -P "$XB" 2>/dev/null; kill -KILL "$XB" 2>/dev/null ) &
  WATCH=$!
fi
wait "$XB"
STATUS=$?
XB=""
[[ -n "$WATCH" ]] && kill "$WATCH" 2>/dev/null

RAW="$(mktemp -d)"
xcrun xcresulttool export attachments --path "$RESULT" --output-path "$RAW" 2>/dev/null
mkdir -p "$OUT"
# manifest.json maps each exported file to its attachment's name. Keep the tour's own
# ("03-conversation", "note-whos-this"), not the recordings and hierarchies Xcode adds.
[[ -f "$RAW/manifest.json" ]] && /usr/bin/python3 - "$RAW" "$OUT" <<'PY'
import json, os, re, shutil, sys
raw, out = sys.argv[1], sys.argv[2]
for test in json.load(open(os.path.join(raw, "manifest.json"))):
    for a in test.get("attachments", []):
        name = a.get("suggestedHumanReadableName") or ""
        m = re.match(r"(\d\d-[a-z0-9-]+|note-[a-z0-9-]+)", name)
        if not m:
            continue
        ext = os.path.splitext(a["exportedFileName"])[1]
        dest = os.path.join(out, m.group(1) + ext)
        shutil.copy(os.path.join(raw, a["exportedFileName"]), dest)
        print(dest)
PY

if [[ "$MODE" == device ]]; then
  # Relaunch the app so the pendant keeps streaming; this is not a test retry.
  relaunch_app || STATUS=6
fi
echo "screens in $OUT ($MODE, test exit $STATUS)"
exit $STATUS
