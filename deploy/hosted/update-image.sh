#!/bin/sh
# Build the image at this commit, load it on the box, move tenants to it, and check each one.
#
#   deploy/hosted/update-image.sh [tenant ...]
#
# With no tenants named, every tenant moves. Name one first to try a new image on it alone.
# Needs: Docker on this machine and ssh access to the box (QUARTER_BOX, default quarter-1).
set -eu
box=${QUARTER_BOX:-quarter-1}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
tenants="$here/tenants.json"
[ -z "$(git -C "$repo" status --porcelain --untracked-files=no)" ] || { echo "commit first: the tag names the commit" >&2; exit 1; }
tag=$(git -C "$repo" rev-parse --short HEAD)-arm64
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

if ! ssh "$box" "sudo docker image inspect quarter-ledger:$tag" >/dev/null 2>&1; then
  docker build -q --platform linux/arm64 -t "quarter-ledger:$tag" "$repo" >/dev/null
  docker save "quarter-ledger:$tag" | gzip | ssh "$box" 'gunzip | sudo docker load'
fi

python3 - "$tenants" "$tag" "$@" <<'EOF'
import json, sys
path, tag, only = sys.argv[1], sys.argv[2], set(sys.argv[3:])
box = json.load(open(path))
names = {t["name"] for t in box["tenants"]}
if only - names:
    sys.exit(f"not in {path}: {', '.join(sorted(only - names))}")
for t in box["tenants"]:
    if not only or t["name"] in only:
        t["tag"] = tag
json.dump(box, open(path, "w"), indent=2)
open(path, "a").write("\n")
EOF

python3 "$here/render.py" "$tenants" "$out" >/dev/null
rsync -a "$out/" "$box:/opt/quarter/"
# Compose does not see a changed litestream.yml, so restart Litestream to read it.
ssh "$box" 'cd /opt/quarter && sudo docker compose up -d >/dev/null && if sudo docker compose config --services | grep -qx litestream; then sudo docker compose restart litestream >/dev/null 2>&1; fi'
domain=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$tenants")

failed=0
for name in $(python3 -c "import json,sys; [print(t['name']) for t in json.load(open(sys.argv[1]))['tenants'] if t['tag'] == sys.argv[2]]" "$tenants" "$tag"); do
  ok=
  for i in 1 2 3 4 5 6 7 8 9; do
    if curl -fsS -m 10 --doh-url https://1.1.1.1/dns-query "https://$name.$domain/healthz" 2>/dev/null | grep -q '"ok": true'; then ok=1; break; fi
    sleep 10
  done
  if [ -n "$ok" ]; then echo "$name: $tag, healthy"; else echo "$name: no answer from https://$name.$domain/healthz" >&2; failed=1; fi
done
exit $failed
