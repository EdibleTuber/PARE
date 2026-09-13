#!/usr/bin/env bash
# kit_pin.sh -- report or change which pare-worker-kit tag every worker pins.
#
# Four workers pin this library by git tag. Nobody should have to REMEMBER who
# is on what: `./kit_pin.sh` answers it, and `./kit_pin.sh vX.Y.Z` moves them
# all at once. Drift between them is the thing worth seeing, so the report
# marks it rather than making you diff four files.
#
# A pin is only as good as the tag existing -- a repo pinned to a tag that was
# never pushed fails at install time, not at commit time, which is a long way
# from the mistake. So a repin refuses a tag that is not on the remote.
#
# Usage:
#   ./kit_pin.sh                # report: who pins what, and is it the latest
#   ./kit_pin.sh v0.2.0         # repin every worker, then report again
set -u   # NOT -e: a repo that is missing or mid-rebase is a result to report.

PROJECTS="${PARE_PROJECTS:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
KIT="${PROJECTS}/pare-worker-kit"
WORKERS=(pare-hardware-mcp pare-frida-mcp pare-mitm-mcp pare-static-mcp)
WANT="${1:-}"

# The pin lives in one dependency line; this is the shape it has to keep.
PIN_RE='pare-worker-kit @ git[+]https://github[.]com/EdibleTuber/pare-worker-kit[.]git@'

current_pin() {   # $1 = repo path -- prints the tag, or nothing if unpinned
  sed -n "s|.*${PIN_RE}\([^\"]*\)\".*|\1|p" "$1/pyproject.toml" 2>/dev/null | head -1
}

if [ ! -d "$KIT/.git" ]; then
  echo "kit_pin: no pare-worker-kit checkout at ${KIT}" >&2
  echo "kit_pin: set PARE_PROJECTS to the directory holding the sibling repos" >&2
  exit 2
fi

# Tags are read from the REMOTE, not from whatever this checkout last fetched:
# the failure being guarded against is pinning to a tag that exists only here.
REMOTE_TAGS="$(git -C "$KIT" ls-remote --tags --refs origin 2>/dev/null \
               | sed 's|.*refs/tags/||' | sort -V)"
if [ -z "$REMOTE_TAGS" ]; then
  echo "kit_pin: could not read tags from the kit's origin -- network, or no remote" >&2
  exit 2
fi
LATEST="$(printf '%s\n' "$REMOTE_TAGS" | tail -1)"

if [ -n "$WANT" ]; then
  if ! printf '%s\n' "$REMOTE_TAGS" | grep -qx -- "$WANT"; then
    echo "kit_pin: ${WANT} is not a tag on the kit's origin. Tags there:" >&2
    printf '  %s\n' $REMOTE_TAGS >&2
    echo "kit_pin: refusing to pin to a tag that does not exist -- a bad pin" >&2
    echo "         fails at install time, a long way from this mistake." >&2
    exit 1
  fi
  for w in "${WORKERS[@]}"; do
    f="${PROJECTS}/${w}/pyproject.toml"
    [ -f "$f" ] || { echo "kit_pin: no pyproject.toml for ${w}, skipped" >&2; continue; }
    before="$(current_pin "${PROJECTS}/${w}")"
    [ "$before" = "$WANT" ] && continue
    sed -i "s|\(${PIN_RE}\)[^\"]*\"|\1${WANT}\"|" "$f"
    # Verify the edit LANDED. A sed whose pattern stopped matching -- because
    # the dependency line was reformatted -- reports success and changes
    # nothing, which is the failure this check exists for.
    after="$(current_pin "${PROJECTS}/${w}")"
    if [ "$after" != "$WANT" ]; then
      echo "kit_pin: ${w} still reads '${after:-<unpinned>}' after the edit --" >&2
      echo "         the dependency line has probably been reformatted." >&2
      exit 1
    fi
    echo "kit_pin: ${w}  ${before:-<unpinned>} -> ${WANT}"
  done
fi

echo
printf '%-20s %-10s %s\n' "WORKER" "PINS" "NOTE"
drift=0
for w in "${WORKERS[@]}"; do
  pin="$(current_pin "${PROJECTS}/${w}")"
  note=""
  if [ -z "$pin" ]; then note="NOT PINNED"; drift=1
  elif [ "$pin" != "$LATEST" ]; then note="behind ${LATEST}"; drift=1
  fi
  printf '%-20s %-10s %s\n' "$w" "${pin:-none}" "$note"
done
echo
echo "kit latest tag on origin: ${LATEST}"
[ "$drift" -eq 0 ] && echo "all four agree, and on the latest tag."
exit "$drift"
