# Deploying ledger

One small Linux VM runs two containers: the gateway and Caddy in front of it for TLS. The ledger is a SQLite file on the VM's disk. A nightly cron job copies it.

## What to buy

| Service | What for | Estimated cost per month |
| --- | --- | --- |
| Small VM: 2 vCPU, 4 GB RAM, 40 GB disk (Hetzner CX22, DigitalOcean or Vultr basic) | Runs the gateway and Caddy | about $5 to $12 |
| VM provider backups or snapshots | Whole-disk restore point | about 20% of the VM price |
| A domain | The address agents call | about $1 (about $10 to $15 a year) |
| TLS certificate | HTTPS | $0 (Caddy gets one from Let's Encrypt) |
| A support email address on the domain (Fastmail, Google Workspace or similar) | Design partners write to it | about $5 to $7 per seat |
| Stripe account | Card billing, only once customers pay by card | $0 to open, fees per charge |

Total to start: about $12 to $25 a month. These are estimates; check each provider's current price before buying.

A Fly.io or Render style host also works: run the same image with a persistent volume mounted at `/data`, and let the host terminate TLS. The VM is recommended because the nightly backup is one cron line and nothing else is needed.

## Deploy on a VM

1. Create the VM with Ubuntu LTS and your SSH key. Open ports 22, 80 and 443 only.
2. Point an `A` record for `ledger.<your-domain>` at the VM's IP.
3. Install Docker: `curl -fsSL https://get.docker.com | sh`.
4. Copy the repository to `/opt/ledger` (for example `git clone` or `rsync`), then build: `cd /opt/ledger && docker build -t quarter-ledger .`
5. Write the caps to `/opt/ledger/config/caps.json` (see the README for its shape). Prices and upstreams for OpenAI, Anthropic, Gemini and OpenRouter are built in; write `prices.json` or `upstreams.json` there only to add vendors or override prices.
6. Write `/opt/ledger/ledger.env`, readable by root only (`chmod 600`):

   ```
   QUARTER_AGENT_KEYS=<long random key per design partner, comma separated>
   QUARTER_ADMIN_KEY=<long random password for finance>
   QUARTER_APPROVAL_WEBHOOK=<optional: a Slack incoming webhook URL, told about each waiting approval>
   QUARTER_DASHBOARD_URL=<optional: the public base URL, so the Slack message links to the dashboard>
   QUARTER_SIGNING_KEY=<optional: unset, serve makes an ed25519 key beside the ledger; keep it apart from the admin key>
   QUARTER_PAYEES=<optional: {"x402": ["0x..."]}; others wait for a person, or add "unlisted": "deny">
   QUARTER_FEED=<optional: https address of the Quarter feed>  QUARTER_FEED_KEY=<its ed25519-pub: key>
   QUARTER_CAPS=/config/caps.json
   QUARTER_CLIENT_IP_HEADER=x-forwarded-for
   # QUARTER_PRICES=/config/prices.json        only if you wrote it in step 5
   # QUARTER_UPSTREAMS=/config/upstreams.json  only if you wrote it in step 5
   ```

   `QUARTER_CLIENT_IP_HEADER` takes each caller's address from the header Caddy sets. Without it every caller has Caddy's address, so ten wrong keys from anyone lock finance out for five minutes, and the audit log records Caddy's address. Set it only when the gateway is reachable through the proxy alone, as here.

   Make keys with `openssl rand -hex 32`. Each value can also be the JSON itself instead of a path, unquoted: Docker keeps quotes as part of the value.

7. Start the gateway. It listens only on the private Docker network:

   ```bash
   docker network create ledger
   docker volume create ledger-data
   docker run -d --name ledger --restart unless-stopped --network ledger \
     --env-file /opt/ledger/ledger.env \
     -v ledger-data:/data -v /opt/ledger/config:/config:ro quarter-ledger
   ```

   If `QUARTER_AGENT_KEYS` is empty the gateway refuses to start, because inside the container it listens on every interface. It also refuses keys shorter than 24 characters. Make them with `openssl rand -hex 24`.

8. Start Caddy with `/opt/ledger/Caddyfile`:

   ```
   ledger.<your-domain> {
       reverse_proxy ledger:8780
   }
   ```

   ```bash
   docker run -d --name caddy --restart unless-stopped --network ledger \
     -p 80:80 -p 443:443 -v /opt/ledger/Caddyfile:/etc/caddy/Caddyfile:ro \
     -v caddy-data:/data caddy:2
   ```

9. Check it: `curl https://ledger.<your-domain>/healthz` answers `{"ok": true}`, and `docker ps` shows the gateway as `healthy`.

Config changes and new keys take effect on `docker restart ledger`. To upgrade, pull the code, rebuild the image, then `docker rm -f ledger` and repeat step 7. The data stays in the volume.

## Rotate keys

- **Agent key:** add the new key to `QUARTER_AGENT_KEYS` next to the old one, restart, let the partner switch, then remove the old key and restart again.
- **Admin key:** change `QUARTER_ADMIN_KEY` and restart. Finance signs in to the dashboard with the new password.
- **Vendor keys** are never stored here. Partners rotate them with the vendor and keep sending them as before.

## Back up and restore

`quarter-ledger backup` uses SQLite's own online backup, so it is safe while the gateway is writing. It writes an owner-only copy and checks its hash chains. Add to root's crontab (`crontab -e`):

```
15 3 * * * d=$(date +\%F) && docker exec ledger quarter-ledger backup /data/backup-$d.db && docker cp ledger:/data/backup-$d.db /var/backups/ledger-$d.db && docker exec ledger rm /data/backup-$d.db && chmod 600 /var/backups/ledger-$d.db && find /var/backups -name 'ledger-*.db' -mtime +30 -delete
```

That keeps 30 nightly copies on the VM. Copy them off the VM too, for example with `rsync` to another machine or to the provider's object storage, since a copy on the same disk does not survive losing the VM.

Anchor the chain heads every night too, and keep the anchors off the VM with the backups. Add `--submit` to also timestamp each digest with OpenTimestamps (only the digest is sent), and then copy `anchor-$d.ots` out as well:

```
20 3 * * * d=$(date +\%F) && docker exec ledger quarter-ledger anchor --out /data/anchor-$d.json && docker cp ledger:/data/anchor-$d.json /var/backups/anchor-$d.json && docker exec ledger rm /data/anchor-$d.json
```

To restore:

```bash
docker stop ledger
docker run --rm -v ledger-data:/data -v /var/backups:/backups alpine \
  sh -c 'cp /backups/ledger-YYYY-MM-DD.db /data/ledger.db && rm -f /data/ledger.db-wal /data/ledger.db-shm && chown 10001 /data/ledger.db && chmod 600 /data/ledger.db'
docker run --rm -v ledger-data:/data quarter-ledger quarter-ledger verify-chain
docker start ledger
```

Calls made after that backup are lost, so take the vendor's own usage report for that gap.

## Point a design partner's agent at it

Give the partner their agent key and the base URL. The agent keeps its own vendor key; only the base URL and three headers change:

```python
client = OpenAI(
    base_url="https://ledger.<your-domain>/openai/v1",
    default_headers={
        "x-quarter-key": "<their agent key>",
        "x-quarter-agent": "researcher",
        "x-quarter-customer": "acme",
    },
)
```

The vendor names in the path (`/openai`, `/anthropic`, `/gemini`, `/openrouter`, plus any keys of `upstreams.json`) pick the vendor. Finance uses `https://ledger.<your-domain>/dashboard`.

Reports and the month-end journal run inside the container:

```bash
docker exec ledger quarter-ledger report --by customer
docker exec ledger quarter-ledger export --month 2026-10 > journal.csv
docker exec ledger quarter-ledger audit-pack --month 2026-10 --out /data/pack.zip && docker cp ledger:/data/pack.zip .
```

Write files to `/data`: the gateway's user cannot write to `/app`, where commands run.

## Not production-grade yet

- **One instance.** SQLite on one disk. No failover; if the VM is down, agents that call through it are down too. Restore loses calls since the last backup.
- **One tenant per instance.** All agent keys share one set of caps, prices and one dashboard. A second customer company needs its own container, volume and hostname. `docs/HOSTED.md` runs many of them on one box.
- **No SSO.** Each person signs in with their own key from `QUARTER_PEOPLE` (README "Teams and roles"). Without it there is one shared admin key, and the approver is only the name given with `approve --as`, `x-quarter-approver` or Slack.
- **Unnamed keys are equal.** A key written `name=qa_...` in `QUARTER_AGENT_KEYS` is bound to that agent. An unnamed key can send any `x-quarter-agent`, and any key can send any `x-quarter-customer` header.
- **No alerting.** Nothing pages anyone when the health check fails; add an uptime monitor on `/healthz`.
