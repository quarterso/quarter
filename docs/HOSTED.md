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
- **Sandbox:** a tenant for prospects. Its upstreams are a mock (`deploy/hosted/mock_upstream.py`) that answers OpenAI and Anthropic chat calls with a canned reply and realistic usage. The gateway runs inside the mock's network namespace, so it reaches the mock on localhost and survives its own restarts. Its network has no way out, so no vendor key is on the box. Its caps are $1 a day per agent and $100 a day in all. The total is high on purpose: every prospect shares it, and at $5 one load test locked the sandbox for everyone until the nightly reset. It is wiped every night and never backed up.

## What it costs

From `quarter/memory/strategy/cheapest-running-2026-11-03.md` and `quarter/memory/strategy/hosting-costs-2026-11-02.md`. Check prices again before buying.

| Stage | Box | Per month |
| --- | --- | --- |
| Sandbox and design partners | Oracle Cloud Always Free ARM (2 cores, 12 GB, public IPv4), running since 2026-11-03 | $0 |
| First paying customers | DigitalOcean $4 (512 MB), then resize to 2 GB at about 10 customers | $4, then $12 |
| About 50 customers | Two 4 GB boxes, about 25 tenants each, Litestream to B2 | about $48 |

- One `quarter-ledger serve` uses about 24 MB idle, 55 to 65 MB after steady traffic, and reached its 96 MB limit at 250 calls at once (measured in Docker on a Mac, arm64; the amd64 image idled at 39 MB under emulation). Nine tenants, the sandbox, its mock and Caddy idled at 260 MB together. After the OS and Docker, the free e2-micro fits about 8 to 10 quiet tenants. The limits add up to more than 1 GB, so a few busy at once can push the box into its own out-of-memory killer.
- The e2-micro's free egress is 1 GB a month on Google's premium tier. Every model reply through the gateway counts against it, so it suits the sandbox and design partners, not paying customers. Past 1 GB the charge falls on the card. Set a Google Cloud budget alert at $1.
- **No paying customer on a free tier.** Free tiers move without notice.
- Cloudflare Tunnel is free. R2 is free up to 10 GB and 1 million uploads a month; at about 10 customers move backups to B2.
- **Why `sync-interval: 10s`:** R2 bills each upload (Class A, $4.50 per million past the free million). Litestream's default of 1 second can make 2.6 million uploads a month for one busy database, about $11.66. At 10 seconds the worst case is 259,200, about $1.17. A restore loses at most the last 10 seconds of calls; tell customers that.

## First box on Oracle (running)

Google closed the founder's new billing accounts as "not in good standing" (D191), so the free box is on Oracle Cloud's Always Free tier instead. It has public IPv4, so the IPv6 traps below do not apply and Slack approvals work.

- **Box:** `quarter-1`, `VM.Standard.A1.Flex` at 2 OCPU and 12 GB (Oracle cut the Always Free Arm allowance to that in 2026; the box was made at 4 and 24 and resized on 2026-11-03, before the trial could disable it), 100 GB boot volume, Ubuntu 24.04 aarch64, `us-phoenix-1`, availability domain 2 (domain 1 answered "Out of host capacity"). VCN `quarter`, subnet `quarter-public`, security list `quarter-ssh-only`: inbound TCP 22 only, password login off. Docker and unattended upgrades installed. `/etc/docker/daemon.json` sets `default-address-pools` to `10.200.0.0/16` in /24s: each tenant has its own network, and Docker's default pools run out at about 27.
- **Image:** build it for arm64 on a Mac with `docker build --platform linux/arm64 -t quarter-ledger:<sha>-arm64 .`. The pinned Caddy, cloudflared and Litestream digests are multi-arch.
- **Access:** `ssh quarter-1` (an entry in `~/.ssh/config` with key `~/.ssh/quarter_oci`). The OCI CLI uses `oci session authenticate` (browser, about an hour) and `OCI_CLI_AUTH=security_token`.
- **Tunnel:** `quarter-hosted`, id `c159f744-fb83-4314-a94e-dfafde882781`. One DNS route per tenant, made by `add-tenant.sh`, rather than the wildcard.
- **Add a customer:** `deploy/hosted/add-tenant.sh <name>`. It adds the tenant with the newest tag, renders, copies, makes its keys on the box (never printed), routes `<name>.quarterfi.com`, starts it and checks `/healthz` from outside, resolving through 1.1.1.1 because the local resolver may still cache the name as missing.
- **Running:** the sandbox, its mock, Caddy, Litestream and `demo` (a backed-up tenant for walkthroughs). The sandbox resets nightly from root's crontab.
- **Backups:** Litestream to the Oracle Object Storage bucket `quarter-backups` (S3 endpoint `https://<namespace>.compat.objectstorage.us-phoenix-1.oraclecloud.com`, path style), with a customer secret key written straight into `env/litestream.env`. A new key is refused with 403 `SignatureDoesNotMatch` for a few minutes. Restore checked on 2026-11-03: `demo` came back from the bucket and `verify-chain` passed. Limits: Always Free allows 50,000 requests a month, which one busy tenant at a 10-second sync can exceed; the bucket is in the same region and account as the box; Litestream copies only `ledger.db`, so `nightly_backup.py` runs from root's crontab at 04:30: it packs `env/*.env`, the tunnel credentials, every non-sandbox tenant's other files (`ledger.signing-key`) and a copy of each ledger made by `quarter-ledger backup` (which checks the hash chains), encrypts the pack with `age` to `backup-recipient.txt` and uploads `secrets/<date>.tar.age` to the Litestream bucket and, when `env/b2.env` exists, to a Backblaze B2 bucket (live: `quarter-backups-qf7k`, us-east-005, keep only the last version, key limited to that bucket), deleting the copy from 30 days before. The private key is only in the founder's Keychain as `quarter-hosted-backup` (made by `backup-key.sh`); read a copy with `security find-generic-password -s quarter-hosted-backup -w | age -d -i - <file> | tar -tzv`. Alerts (live since 2026-11-04 UTC, checks "quarter nightly backup" 1 day + 2 h grace and "quarter litestream" 1 h + 30 min, emailing the founder): with `env/healthchecks.env` (`HC_BACKUP_URL`, `HC_LITESTREAM_URL`), the nightly run pings healthchecks.io on success and `/fail` on any error, and an hourly `--litestream` run pings when Litestream logged no error in the last 65 minutes. Put keys on the box with `deploy/hosted/put-env.sh <file> VAR...`, which asks with hidden input.
- **Measured on the box** (4 OCPU, before the resize): about 23 MB per idle tenant, 39 MB after 2,000 calls; through Caddy to the sandbox and its mock, 158 decided calls a second with 10 at once (p50 46 ms, p99 111 ms). One load test spent the sandbox's old $5 daily total and locked it for every prospect, so that cap is now $100.
- **Reclaim risk:** Oracle may stop an Always Free machine that stays nearly idle for 7 days (95th percentile CPU, network and memory all under 20%). Upgrading the account to Pay As You Go keeps Always Free resources free, but lets Oracle charge the card for anything else. Open in D191.
- **Scale path:** `quarter/memory/strategy/infra-expansion-2026-11-03.md`.

## First box on Google Cloud (not used)

The free e2-micro, from nothing. Steps marked **by hand** only the founder can do. Commands checked against Google's gcloud reference on 2026-11-03, not yet run.

1. **By hand:** create a Google Cloud billing account with your card in the console (Billing). Google may hold $0 to $1. Install `gcloud`, run `gcloud auth login`, and note the account id from `gcloud billing accounts list`.
2. Project, billing and APIs (the project id must be unique across Google Cloud):

   ```bash
   P=quarter-hosted-1 B=<billing account id> Z=us-east1-b
   gcloud projects create $P
   gcloud billing projects link $P --billing-account=$B
   gcloud config set project $P
   gcloud services enable compute.googleapis.com billingbudgets.googleapis.com
   ```

3. Budget alert at $1. It emails the billing admins at 50% and 100%; it does not stop spending. The amount must be in the billing account's currency:

   ```bash
   gcloud billing budgets create --billing-account=$B --display-name=quarter-1usd \
     --budget-amount=1USD --filter-projects=projects/$P \
     --threshold-rule=percent=0.5 --threshold-rule=percent=1.0
   ```

4. IPv6-only network, SSH open, and the VM (free: e2-micro, standard disk up to 30 GB, us-east1, us-central1 or us-west1):

   ```bash
   gcloud compute networks create quarter --subnet-mode=custom
   gcloud compute networks subnets create quarter-v6 --network=quarter --region=us-east1 \
     --stack-type=IPV6_ONLY --ipv6-access-type=EXTERNAL
   gcloud compute firewall-rules create quarter-ssh --network=quarter --direction=INGRESS \
     --action=allow --rules=tcp:22 --source-ranges=::/0
   gcloud compute images describe-from-family ubuntu-2404-lts-amd64 --project=ubuntu-os-cloud
   gcloud compute instances create quarter-1 --zone=$Z --machine-type=e2-micro \
     --subnet=quarter-v6 --stack-type=IPV6_ONLY --ipv6-network-tier=PREMIUM \
     --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
     --boot-disk-size=30GB --boot-disk-type=pd-standard
   ```

   If `describe-from-family` finds no such family, pick the current one from `gcloud compute images list --filter=family~ubuntu --project=ubuntu-os-cloud`.
5. SSH needs IPv6 on your own network: `curl -6 https://ifconfig.co` must answer. Then `gcloud compute ssh quarter-1 --zone=$Z`. Google's docs do not say whether `--tunnel-through-iap` reaches a VM with only an external IPv6 address, and a forum answer says it does not, so do not count on it.
6. **By hand, on your machine:** `cloudflared tunnel login` opens your browser; choose quarterfi.com. Then follow "Set up the box once" from step 2.
7. Checks only this VM can answer:
   - `time sudo docker run --rm --network quarter_edge --entrypoint python quarter-ledger:<tag> -c "import socket; socket.create_connection(('api.openai.com', 443), 10)"` takes well under a second. Containers on the box's networks also have IPv4 addresses that lead nowhere; if this takes about 10 seconds, calls try IPv4 first and hang.
   - `sudo docker compose logs cloudflared` shows registered tunnel connections, and `https://sandbox.quarterfi.com/healthz` answers from your browser.
   - `free -m` after a day of use: if available memory stays under 150 MB, move tenants off.

## Set up the box once

1. **Make the VM.** On Google Cloud, as in "First box" above. On a paid box with IPv4, any Ubuntu VM works; close every inbound port but 22.
2. **Install Docker:** `curl -fsSL https://get.docker.com | sh`. Docker turns on IPv6 forwarding, and Linux then ignores router advertisements, which can drop the VM's IPv6 route. On the IPv6-only VM, keep them: `echo 'net.ipv6.conf.ens4.accept_ra = 2' > /etc/sysctl.d/90-ra.conf && sysctl --system` (the interface may not be `ens4`; see `ip -6 route`).
3. **Make the tunnel** on your own machine: `cloudflared tunnel login`, `cloudflared tunnel create quarter-hosted`, then `cloudflared tunnel route dns quarter-hosted '*.quarterfi.com'`. That prints the tunnel id and writes `~/.cloudflared/<id>.json`, the tunnel's secret. If `route dns` refuses the wildcard, add a proxied `CNAME` for `*` to `<id>.cfargotunnel.com` in the Cloudflare dashboard. Records that already exist, such as the website's, still win over the wildcard.
4. **Write `deploy/hosted/tenants.json`** from `tenants.example.json`: the tunnel id, `"ipv6_only": true` on the e2-micro (false on a box with IPv4), and the tenants. Each has a `name` (a lowercase DNS label), the image `tag`, and optionally `"sandbox": true`, `"memory": "128m"` and `"plan": "paid"`. Free-plan tenants (the default) get `QUARTER_FREE_DECISIONS=10000` a month (D185); past it replies carry `x-quarter-free-limit: exceeded` and calls are still decided (D190).
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

Add it to `tenants.json`, render, `rsync` the output, write its `env/<tenant>.env`, then `docker compose up -d`. Compose starts the new tenant and recreates Caddy and Litestream, whose networks and volumes changed, so every tenant drops off for a second or two. Other tenants keep running.

## Update the image

On the Oracle box: `deploy/hosted/update-image.sh [tenant ...]` builds the arm64 image at the current commit, loads it on the box, moves the named tenants (or all) to it, renders, starts them and checks each `/healthz` from outside. Move one tenant first, then the rest. By hand, as below:

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
0 4 * * * cd /opt/quarter && docker compose rm -sf sandbox sandbox-mock && docker volume rm quarter_sandbox; docker compose up -d sandbox sandbox-mock
```

Prospects then start from an empty ledger. The sandbox comes back up even if the wipe fails. Give them the sandbox's agent key and `https://sandbox.quarterfi.com/openai/v1` or `/anthropic` as the base URL; the agent key also works as the vendor key there.

## Limits

- **Tested locally, not on Google Cloud.** The rendered files pass `docker compose config`, `caddy validate` and `cloudflared tunnel ingress validate`. Run in Docker on a Mac (last on 2026-11-03): Caddy's routing by host, a held call approved after 80 seconds, the 110-second cap, the dashboard sign-in and its lockout per `cf-connecting-ip`, tenant isolation, the sandbox with no way out, the memory limit, a sandbox crash and restart, the sandbox reset, adding a tenant and a new image. An earlier local run copied and restored with Litestream against a local S3 server. The tunnel, the IPv6-only VM, the `accept_ra` step, R2 and B2 have not been run.
- **One box is one point of failure.** If it is down, every tenant on it is down. Add an uptime check on each `/healthz`.
- **Root on the box reaches every customer's vendor keys.** SSH keys only, unattended security updates, env files `chmod 600`.
- **Slack has no IPv6.** On the IPv6-only VM, Slack approvals and the approval webhook cannot reach Slack.
- Caddy, cloudflared and Litestream have no memory limit. Caddy used 14 MB idle and up to 75 MB under 250 calls at once; Litestream used 12 MB; cloudflared was not measured.
