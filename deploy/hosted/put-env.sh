#!/bin/sh
# Write secret values into an env file on the box without showing them anywhere.
#
#   deploy/hosted/put-env.sh <file> VAR [VAR...] [-- VAR=value ...]
#
# Asks for each VAR with hidden input, then writes /opt/quarter/env/<file> (replacing it)
# over ssh, owner only. Values after -- are not secret and are written as given.
# Examples:
#   deploy/hosted/put-env.sh b2.env LITESTREAM_ACCESS_KEY_ID LITESTREAM_SECRET_ACCESS_KEY -- \
#     LITESTREAM_BUCKET=<bucket> LITESTREAM_ENDPOINT=https://s3.<region>.backblazeb2.com LITESTREAM_REGION=<region>
#   deploy/hosted/put-env.sh healthchecks.env HC_BACKUP_URL HC_LITESTREAM_URL
set -eu
file=${1:?usage: put-env.sh <file> VAR [VAR...] [-- VAR=value ...]}
shift
box=${QUARTER_BOX:-quarter-1}
case $file in */*|.*) echo "file must be a plain name such as b2.env" >&2; exit 1;; esac

body=""
while [ $# -gt 0 ] && [ "$1" != "--" ]; do
  printf '%s (hidden): ' "$1" >&2
  stty -echo; IFS= read -r value; stty echo; echo >&2
  [ -n "$value" ] || { echo "$1 is empty; nothing written" >&2; exit 1; }
  body="$body$1=$value
"
  shift
done
[ "${1:-}" = "--" ] && shift
for pair in "$@"; do body="$body$pair
"; done

printf '%s' "$body" | ssh "$box" "umask 077; cat > /opt/quarter/env/$file.tmp && mv /opt/quarter/env/$file.tmp /opt/quarter/env/$file && chmod 600 /opt/quarter/env/$file"
value=; body=
echo "Wrote /opt/quarter/env/$file on $box ($(ssh "$box" "wc -l < /opt/quarter/env/$file") lines)."
