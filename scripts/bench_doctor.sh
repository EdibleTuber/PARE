#!/usr/bin/env bash
# bench_doctor.sh -- run this ON THE PI, from its own shell, when something the
# agent found has not shown up on the bench screen.
#
# Spec: docs/superpowers/specs/2026-09-06-arcticbase-artifacts-design.md §10.2.
#
# There are five candidates when something does not arrive: tailnet, ArcticBase,
# the daemon, the worker, the drive. This names which one, in five lines.
#
# It is DELIBERATELY daemon-independent and dependency-free: no Python, no venv,
# no PARE import, no /health call. It has to work in the state you most need it,
# which is the daemon being down -- and it has to work when the status page
# itself is the thing that failed.
#
# Usage:
#   ./bench_doctor.sh                       # uses the defaults below
#   PARE_SERVER=http://100.82.222.92:2929 PARE_ARTIFACT_ROOT=/mnt/bench-store \
#     WORKER_PORT=9100 ./bench_doctor.sh
set -u   # NOT -e: a failing probe is a result to report, not a reason to stop.

SERVER="${PARE_SERVER:-http://100.82.222.92:2929}"
ARTIFACT_ROOT="${PARE_ARTIFACT_ROOT:-/mnt/bench-store}"
WORKER_PORT="${WORKER_PORT:-9100}"
DRIVE_ID_FILE="${ARTIFACT_ROOT}/.bench-store-id"

pass() { printf '  \033[32mOK  \033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; PROBLEMS=$((PROBLEMS+1)); }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }
PROBLEMS=0

echo "PARE bench doctor -- $(date -u '+%Y-%m-%dT%H:%M:%SZ') (this Pi's clock; it has no RTC)"
echo

# --- 1. tailnet -------------------------------------------------------------
echo "1. tailnet"
if ! command -v tailscale >/dev/null 2>&1; then
  fail "tailscale is not installed or not on PATH"
elif ! ts_out="$(tailscale status 2>&1)"; then
  fail "tailscale status failed: ${ts_out%%$'\n'*}"
else
  pass "tailnet up ($(printf '%s' "$ts_out" | grep -c . ) peers listed)"
fi

# --- 2. ArcticBase ----------------------------------------------------------
echo "2. arcticbase ($SERVER)"
code="$(curl -s -m3 -o /dev/null -w '%{http_code}' "$SERVER/api/health" 2>/dev/null)"
if [ "$code" = "200" ]; then
  pass "answering on /api/health"
elif [ "$code" = "000" ]; then
  fail "no answer (down, wrong address, or blocked) -- curl got no HTTP status"
else
  fail "answered $code on /api/health"
fi

# --- 3. the daemon ----------------------------------------------------------
# Read the heartbeat straight out of ArcticBase rather than asking the daemon:
# asking the daemon whether the daemon is up cannot report the interesting case.
echo "3. daemon heartbeat"
hb_url="$SERVER/api/workbenches/pare-daemon-status/objects"
if [ "$code" != "200" ]; then
  warn "not checked -- ArcticBase is not answering (fix 2 first)"
else
  objs="$(curl -s -m3 "$hb_url" 2>/dev/null)"
  case "$objs" in
    *'"title":"heartbeat"'*|*'"title": "heartbeat"'*)
      pass "a heartbeat object exists (the status page reports its age)" ;;
    "") fail "could not list the daemon-status workbench" ;;
    *)  fail "no heartbeat object -- the daemon has never written one" ;;
  esac
fi

# --- 4. the worker ----------------------------------------------------------
echo "4. worker on this Pi (port $WORKER_PORT)"
if command -v ss >/dev/null 2>&1; then
  if ss -tln 2>/dev/null | grep -q ":${WORKER_PORT}\b"; then
    pass "something is listening on :$WORKER_PORT"
  else
    fail "nothing is listening on :$WORKER_PORT -- the worker unit is not running"
  fi
else
  warn "ss not available; cannot check the listener"
fi

# --- 5. the drive -----------------------------------------------------------
echo "5. artifact drive ($ARTIFACT_ROOT)"
if ! mountpoint -q "$ARTIFACT_ROOT" 2>/dev/null; then
  fail "$ARTIFACT_ROOT is not a mountpoint -- the drive is absent or unmounted"
else
  free="$(df -h --output=avail "$ARTIFACT_ROOT" 2>/dev/null | tail -1 | tr -d ' ')"
  # A read-only remount is what ext4 does by default on error, and it looks
  # like a healthy mount to df.
  probe="$ARTIFACT_ROOT/.doctor-write-probe.$$"
  if touch "$probe" 2>/dev/null; then
    rm -f "$probe"
    pass "mounted, writable, $free free"
  else
    # Two causes, opposite fixes, and this used to GUESS between them --
    # "likely remounted read-only after an error" is what it said when the
    # real cause on a freshly mounted drive was root:root ownership. Distinguish
    # them instead: the mount options say which it is.
    #
    # (That message was also being passed as two arguments to a function that
    # prints only "$1", so half of it never reached the screen.)
    opts="$(findmnt -n -o OPTIONS "$ARTIFACT_ROOT" 2>/dev/null)"
    case ",$opts," in
      *,ro,*)
        fail "mounted READ-ONLY ($free free). ext4 remounts ro on error by default, so this usually means a write failed; check dmesg before writing anything else here." ;;
      *)
        fail "mounted rw with $free free, but not writable by $(id -un): $(stat -c '%U:%G %a' "$ARTIFACT_ROOT" 2>/dev/null). A fresh mount is owned by root; chown it to the user the worker runs as." ;;
    esac
  fi
  if [ -r "$DRIVE_ID_FILE" ]; then
    pass "drive id $(head -c 64 "$DRIVE_ID_FILE" 2>/dev/null | tr -d '\n')"
  else
    warn "no $DRIVE_ID_FILE -- cannot tell this drive from another project's"
  fi
fi

echo
if [ "$PROBLEMS" -eq 0 ]; then
  echo "No problems found. If a finding still has not appeared, it was never published:"
  echo "check the daemon's own /health and the agent transcript."
else
  echo "$PROBLEMS problem(s) above. Fix the FIRST one -- the later probes may only"
  echo "be failing because of it."
fi
exit 0
