#!/usr/bin/env bash
# Deploy the bench files from a git checkout ON THE PI, with provenance.
#
#   ./scripts/bench_deploy.sh --check    # read-only, no sudo, exits 1 on drift
#   sudo ./scripts/bench_deploy.sh       # install what differs, then re-check
#
# Why this exists: the first deployment was files streamed over SSH into a
# staging directory and copied into /opt/pare by hand. Nothing recorded which
# commit they came from, so "what is running?" could only be answered by
# sha256-ing each file against the repo -- and the staging copy silently drifted
# two commits BEHIND the running one, which made following the documented
# install step a downgrade.
#
# So: deploy from a checkout whose HEAD can be read, and stamp it.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST=/opt/pare
UNIT_DIR=/etc/systemd/system
STAMP="$DEST/DEPLOYED_FROM"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# file-in-repo -> destination
FILES=(
  "bench/__init__.py:$DEST/bench/__init__.py"
  "bench/status_server.py:$DEST/bench/status_server.py"
  "scripts/bench_doctor.sh:$DEST/scripts/bench_doctor.sh"
  "bench/systemd/pare-bench-status.service:$UNIT_DIR/pare-bench-status.service"
  "bench/systemd/pare-bench-kiosk.service:$UNIT_DIR/pare-bench-kiosk.service"
  "deploy/polkit/50-pare-poweroff.rules:/etc/polkit-1/rules.d/50-pare-poweroff.rules"
)

if [ ! -d "$REPO/.git" ]; then
  echo "ERROR: $REPO is not a git checkout. This script deploys FROM a checkout"
  echo "on purpose -- a copied file cannot tell you which commit it came from."
  exit 2
fi

HEAD_SHA="$(git -C "$REPO" rev-parse --short HEAD)"
DIRTY="$(git -C "$REPO" status --porcelain | wc -l)"
echo "checkout $REPO @ $HEAD_SHA${DIRTY:+ ($DIRTY modified)}"
[ -r "$STAMP" ] && echo "deployed: $(head -1 "$STAMP")" || echo "deployed: no provenance stamp yet"
echo

drift=0
declare -a NEED_FILE NEED_UNIT
for pair in "${FILES[@]}"; do
  src="$REPO/${pair%%:*}"; dst="${pair##*:}"
  a=$(sha256sum "$src" 2>/dev/null | cut -c1-12)
  b=$(sha256sum "$dst" 2>/dev/null | cut -c1-12)
  if [ -z "$a" ]; then
    printf '  %-44s MISSING IN REPO\n' "${pair%%:*}"; drift=1; continue
  fi
  if [ "$a" = "$b" ]; then
    printf '  %-44s current\n' "$dst"
  else
    printf '  %-44s STALE (repo %s, deployed %s)\n' "$dst" "$a" "${b:-absent}"
    drift=1
    case "$dst" in
      "$UNIT_DIR"/*) NEED_UNIT+=("$src:$dst") ;;
      *)             NEED_FILE+=("$src:$dst") ;;
    esac
  fi
done

echo
if [ "$drift" -eq 0 ]; then
  echo "Everything deployed matches $HEAD_SHA."
  exit 0
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  echo "Drift above. Re-run with sudo (no --check) to install."
  exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Installing needs root. Re-run: sudo $0"
  exit 1
fi

for pair in "${NEED_FILE[@]:-}"; do
  [ -z "$pair" ] && continue
  src="${pair%%:*}"; dst="${pair##*:}"
  install -D -m "$([ "${dst##*.}" = "sh" ] && echo 755 || echo 644)" "$src" "$dst"
  echo "  installed $dst"
done

reload=0
for pair in "${NEED_UNIT[@]:-}"; do
  [ -z "$pair" ] && continue
  src="${pair%%:*}"; dst="${pair##*:}"
  install -m 644 "$src" "$dst"
  echo "  installed $dst"
  reload=1
done
[ "$reload" -eq 1 ] && { systemctl daemon-reload; echo "  systemctl daemon-reload"; }

# Stamp BEFORE restarting, so a unit that fails to come back still leaves a
# truthful record of what was put there.
printf '%s  deployed %s from %s\n' "$HEAD_SHA" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$REPO" > "$STAMP"
chmod 644 "$STAMP"
echo "  stamped $STAMP"

# Restart the status page whenever anything it runs changed. The kiosk only
# needs a restart if its own unit changed -- it loads the page over HTTP, so a
# status-page update reaches it on the next poll without touching the browser.
systemctl restart pare-bench-status && echo "  restarted pare-bench-status"
for pair in "${NEED_UNIT[@]:-}"; do
  case "$pair" in *pare-bench-kiosk*) systemctl restart pare-bench-kiosk &&
    echo "  restarted pare-bench-kiosk" ;; esac
done

echo
echo "Re-checking:"
exec "$0" --check
