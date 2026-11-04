# Hosting ledger for many customers

One box runs many customer companies. Each tenant is one container of the same image, with its own volume, its own env file and its own hostname, `<tenant>.quarterfi.com`. The ledger itself stays single tenant.

```
agent ──https──> Cloudflare ──tunnel──> cloudflared ──> Caddy ──Host header──> acme:8780
                                                                           └──> sandbox:8780 ── its mock
Litestream ──every 10 s──> R2 or B2 (every tenant but the sandbox)
```

- **Ingress:** `cloudflared` dials out to Cloudflare, so the box needs no public IPv4 and no open port. It sends `*.quarterfi.com` to Caddy, which routes by host to each tenant over plain HTTP. Each tenant has its own Docker network, which only Caddy joins, so no tenant can reach another's container directly. TLS ends at Cloudflare. Unknown hosts get 404.
- **Wait cap:** Cloudflare cuts a proxied request at about 125 seconds. The kit sets `QUARTER_MAX_WAIT=110`, so `x-quarter-wait` holds a call at most 110 seconds. Self-hosted gateways keep 300.
- **Memory:** each tenant has a limit (96 MB unless set), so one tenant cannot starve the others.
- **Sandbox:** a tenant for prospects. Its upstreams are a mock (`deploy/hosted/mock_upstream.py`) that answers OpenAI and Anthropic chat calls with a canned reply and realistic usage. Its network has no way out, so no vendor key is on the box. Its caps are $1 a day per agent and $5 a day in all. It is wiped every night and never backed up.

## What it costs

From `quarter/memory/strategy/cheapest-running-2026-11-03.md` and `quarter/memory/strategy/hosting-costs-2026-11-02.md`. Check prices again before buying.

| Stage | Box | Per month |
| --- | --- | --- |
| Sandbox and design partners | Google Cloud e2-micro (1 GB), IPv6 only, in us-east1, us-central1 or us-west1 | $0 |
| First paying customers | DigitalOcean $4 (512 MB), then resize to 2 GB at about 10 customers | $4, then $12 |
| About 50 customers | Two 4 GB boxes, about 25 tenants each, Litestream to B2 | about $48 |

- One `quarter-ledger serve` uses about 47 MB. After the OS and Docker, the free e2-micro fits about 8 to 10 tenants.
- The e2-micro's free egress is 1 GB a month on Google's premium tier. Every model reply through the gateway counts against it, so it suits the sandbox and design partners, not paying customers. Past 1 GB the charge falls on the card. Set a Google Cloud budget alert at $1.
- **No paying customer on a free tier.** Free tiers move without notice.
- Cloudflare Tunnel is free. R2 is free up to 10 GB and 1 million uploads a month; at about 10 customers move backups to B2.
- **Why `sync-interval: 10s`:** R2 bills each upload (Class A, $4.50 per million past the free million). Litestream's default of 1 second can make 2.6 million uploads a month for one busy database, about $11.66. At 10 seconds the worst case is 259,200, about $1.17. A restore loses at most the last 10 seconds of calls; tell customers that.

## Set up the box once

1. **Make the VM.** On Google Cloud: an e2-micro in a free region, Ubuntu LTS, 30 GB standard disk, on a subnet with external IPv6 and the VM's stack type IPv6 only. With no IPv4 at all, calls to IPv4 addresses fail at once instead of hanging. Reach it with `gcloud compute ssh --tunnel-through-iap` if your network has no IPv6. On a paid box with IPv4, any Ubuntu VM works; close every inbound port but 22.
2. **Install Docker:** `curl -fsSL https://get.docker.com | sh`. Docker turns on IPv6 forwarding, and Linux then ignores router advertisements, which can drop the VM's IPv6 route. On the IPv6-only VM, keep them: `echo 'net.ipv6.conf.ens4.accept_ra = 2' > /etc/sysctl.d/90-ra.conf && sysctl --system` (the interface may not be `ens4`; see `ip -6 route`).
3. **Make the tunnel** on your own machine: `cloudflared tunnel login`, `cloudflared tunnel create quarter-hosted`, then `cloudflared tunnel route dns quarter-hosted '*.quarterfi.com'`. That prints the tunnel id and writes `~/.cloudflared/<id>.json`, the tunnel's secret. If `route dns` refuses the wildcard, add a proxied `CNAME` for `*` to `<id>.cfargotunnel.com` in the Cloudflare dashboard. Records that already exist, such as the website's, still win over the wildcard.
4. **Write `deploy/hosted/tenants.json`** from `tenants.example.json`: the tunnel id, `"ipv6_only": true` on the e2-micro (false on a box with IPv4), and the tenants. Each has a `name` (a lowercase DNS label), the image `tag`, and optionally `"sandbox": true` and `"memory": "128m"`.
5. **Build the image and the files** on your machine. github.com and ghcr.io have no IPv6, so the IPv6-only VM cannot build the image (the Dockerfile mounts uv from ghcr.io):

   ```bash
   tag=$(git rev-parse --short HEAD)
   docker build --platform linux/amd64 -t quarter-ledger:$tag .
   python3 deploy/hosted/render.py deploy/hosted/tenants.json out
   docker save quarter-ledger:$tag | gzip | ssh <box> 'gunzip | sudo docker load'
   rsync -a out/ <box>:/opt/quarter/
   ```

6. **Place the secrets** on the box, all `chmod 600`. Nothing in the repository holds them.
   - `/opt/quarter/cloudflared/credentials.json`: the tunnel's `<id>.json`. cloudflared runs as uid 65532, so `chown 65532` it.
   - `/opt/quarter/env/<tenant>.env` for each tenant, as in `docs/DEPLOY.md` step 6. The kit sets `QUARTER_MAX_WAIT`, `QUARTER_DASHBOARD_URL` and `QUARTER_CLIENT_IP_HEADER` (Cloudflare's `cf-connecting-ip`, so a lockout hits the caller, not every caller) itself. The sandbox's file needs only `QUARTER_AGENT_KEYS` and `QUARTER_ADMIN_KEY`; the kit sets its upstreams, vendor keys and caps.
   - `/opt/quarter/env/litestream.env`:

     ```
     LITESTREAM_ACCESS_KEY_ID=<bucket key id>
     LITESTREAM_SECRET_ACCESS_KEY=<bucket key secret>
     LITESTREAM_BUCKET=<bucket name>
     # R2: https://<account id>.r2.cloudflarestorage.com and region auto
     # B2: https://s3.<region>.backblazeb2.com and that region, such as us-west-004
     LITESTREAM_ENDPOINT=<endpoint>
     LITESTREAM_REGION=<region>
     ```

7. **Start it:** `cd /opt/quarter && sudo docker compose up -d`. Check `curl https://acme.quarterfi.com/healthz` answers `{"ok": true}` and `docker compose ps` shows each tenant `healthy`. Test each new hostname with that customer's key before handing it over: a wrong site block sends one customer's agents to another's ledger.

## Add a tenant

Add it to `tenants.json`, render, `rsync` the output, write its `env/<tenant>.env`, then `docker compose up -d && docker compose restart caddy litestream`. Compose starts the new tenant; the restart makes Caddy and Litestream read the new files. Other tenants keep running.

## Update the image

Build and load the new tag as in step 5, change `tag` for the tenants to move, render, `rsync`, then `docker compose up -d`. Each tenant whose tag changed is recreated; its data stays in its volume. Move one tenant first, then the rest.

## Restore one tenant

Litestream keeps every tenant's database in the bucket under the tenant's name. To bring `acme` back:

```bash
cd /opt/quarter
docker compose stop acme litestream
docker compose run --rm --no-deps --entrypoint sh acme -c \
  'mkdir -p /data/before-restore && mv /data/ledger.db* /data/.ledger.db-litestream /data/before-restore/'
docker compose run --rm --no-deps litestream restore -config /etc/litestream.yml \
  -o /data/acme/ledger.db /data/acme/ledger.db
docker compose run --rm --no-deps --entrypoint sh acme -c 'chmod 600 /data/ledger.db'
docker compose run --rm --no-deps acme quarter-ledger verify-chain
docker compose up -d acme litestream
```

The old files stay in `/data/before-restore` until you delete them. Calls in the last 10 seconds before the loss are not in the copy; take the vendor's usage report for that gap. To rebuild a whole box, set it up as above, run the restore for each tenant before its first start, then start everything. Keep the nightly `quarter-ledger backup` from `docs/DEPLOY.md` too, since it checks both hash chains in the copy.

## Reset the sandbox every night

Add to root's crontab (`crontab -e`). It wipes only the sandbox's volume:

```
0 4 * * * cd /opt/quarter && docker compose rm -sf sandbox sandbox-mock && docker volume rm quarter_sandbox && docker compose up -d sandbox sandbox-mock
```

Prospects then start from an empty ledger. Give them the sandbox's agent key and `https://sandbox.quarterfi.com/openai/v1` or `/anthropic` as the base URL; the agent key also works as the vendor key there.

## Limits

- **Tested locally, not on Google Cloud.** The rendered files pass `docker compose config`, `caddy validate` and `cloudflared tunnel ingress validate`. Caddy's routing, the sandbox and its mock, the sandbox reset, and a Litestream copy and restore against a local S3 server were run in Docker on a Mac. The tunnel, the IPv6-only VM, the `accept_ra` step, R2 and B2 have not been run.
- **One box is one point of failure.** If it is down, every tenant on it is down. Add an uptime check on each `/healthz`.
- **Root on the box reaches every customer's vendor keys.** SSH keys only, unattended security updates, env files `chmod 600`.
- **Slack has no IPv6.** On the IPv6-only VM, Slack approvals and the approval webhook cannot reach Slack.
- Caddy, cloudflared and Litestream have no memory limit. Caddy used 14 MB in the local run; the other two were not measured.
