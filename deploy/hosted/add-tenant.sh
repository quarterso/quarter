#!/bin/sh
# Add a customer company to the hosted box, from your machine.
#
#   deploy/hosted/add-tenant.sh <name> [box]
#
# Adds <name> to deploy/hosted/tenants.json with the newest tag there, renders,
# copies the files, makes the tenant's keys on the box (never printed here), routes
# <name>.<domain> to the tunnel, starts it and checks /healthz from outside.
# Needs: ssh access to the box (default host quarter-1) and `cloudflared tunnel login` done.
set -eu
name=${1:?usage: add-tenant.sh <name> [box]}
box=${2:-quarter-1}
here=$(cd "$(dirname "$0")" && pwd)
tenants="$here/tenants.json"
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT

python3 - "$tenants" "$name" <<'EOF'
import json, sys
path, name = sys.argv[1], sys.argv[2]
box = json.load(open(path))
if any(t["name"] == name for t in box["tenants"]):
    sys.exit(f"{name} is already in {path}")
tag = next(t["tag"] for t in reversed(box["tenants"]))
box["tenants"].append({"name": name, "tag": tag})
json.dump(box, open(path, "w"), indent=2)
open(path, "a").write("\n")
EOF

python3 "$here/render.py" "$tenants" "$out" >/dev/null
domain=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['domain'])" "$tenants")
tunnel=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['tunnel'])" "$tenants")

cloudflared tunnel route dns "$tunnel" "$name.$domain" 2>&1 | grep -v WRN || true
rsync -a "$out/" "$box:/opt/quarter/"
ssh "$box" "set -e; cd /opt/quarter; umask 077
  [ -f env/$name.env ] || printf 'QUARTER_AGENT_KEYS=%s\nQUARTER_ADMIN_KEY=%s\n' \"\$(openssl rand -hex 24)\" \"\$(openssl rand -hex 24)\" > env/$name.env
  chmod 600 env/$name.env
  # Start only the new tenant, then join Caddy to its network and reload Caddy in place, so
  # other tenants and their held calls are not cut. Litestream restarts to pick up the new ledger.
  sudo docker compose up -d --no-recreate $name >/dev/null 2>&1
  if sudo docker compose config --services | grep -qx litestream; then sudo docker compose up -d --no-deps litestream >/dev/null 2>&1; fi
  caddy=\$(sudo docker compose ps -q caddy)
  net=\$(sudo docker inspect -f '{{range \$k, \$v := .NetworkSettings.Networks}}{{\$k}} {{end}}' \$(sudo docker compose ps -q $name) | tr ' ' '\n' | grep -v '^\$' | head -1)
  sudo docker network connect \$net \$caddy 2>/dev/null || true
  sudo docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || sudo docker compose up -d caddy >/dev/null 2>&1"

# Resolve through 1.1.1.1: this machine may have cached the name as missing.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS -m 10 --doh-url https://1.1.1.1/dns-query "https://$name.$domain/healthz" 2>/dev/null | grep -q '"ok": true'; then
    echo "$name is live at https://$name.$domain"
    echo "Its keys are in /opt/quarter/env/$name.env on $box. Send them to the customer over a private channel."
    exit 0
  fi
  sleep 10
done
echo "$name did not answer https://$name.$domain/healthz within 2 minutes; check: ssh $box 'cd /opt/quarter && sudo docker compose ps'" >&2
exit 1
