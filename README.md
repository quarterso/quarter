# Quarter

Spending caps, human approvals and a tamper-evident ledger for what AI agents spend. Pre-release. https://quarterfi.com

## Install

- From source: `git clone https://github.com/quarterso/quarter && cd quarter && uv sync`
- From PyPI: `uv tool install quarter-ledger` or `pipx install quarter-ledger` (the `quarter-ledger` command; `quarter-ledger demo` works from it too)
- As a container: run `quarter-ledger init` once for a `.env` and `caps.json` with fresh keys, then, in that folder, `docker run -p 8780:8780 --env-file .env -v "$PWD/caps.json:/app/caps.json:ro" -v quarter:/data ghcr.io/quarterso/quarter`. Without agent keys the gateway refuses to listen beyond localhost.

## Try it in two minutes

```bash
uvx quarter-ledger demo            # from PyPI, nothing else to install
uv sync && uv run quarter-ledger demo  # or in a checkout
```

It runs on this machine only, with made-up keys and a mock vendor, so it costs nothing. It starts the gateway, sends calls as two agents for two customers, holds one large call until a named person approves it, and books a sample LiteLLM bill through connect mode. Then it prints the dashboard address with its sign-in, and the commands for the journal and the audit pack. Press Ctrl+C to stop it. `--dir <folder>` keeps it in a folder you choose.

## Try the hosted sandbox, nothing to install

`https://sandbox.quarterfi.com` is a Quarter gateway in front of a mock vendor: no model is called and nothing costs anything. Everyone trying it shares it, and it is wiped every night (04:00 Pacific), so send nothing real.

- Agent key: `qa_try6d027c751e3aed17512402058c`
- Approver sign-in for the dashboard (any username): `try-approver-b7f88ef061bd`

```python
from openai import OpenAI

client = OpenAI(base_url="https://sandbox.quarterfi.com/openai/v1", api_key="qa_try6d027c751e3aed17512402058c")
reply = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
                                       extra_headers={"x-quarter-customer": "acme", "x-quarter-task": "first-try"})
print(reply.choices[0].message.content)
```

Anthropic works the same way at `https://sandbox.quarterfi.com/anthropic`. Ask `gpt-4o` for `max_tokens=8000` (up to about $0.08, over the sandbox's $0.05 line) and the call is held for a person: the SDK raises `PermissionDeniedError` with an `approval_id`. Approve it at `https://sandbox.quarterfi.com/dashboard` with the approver sign-in, or with `curl -X POST -H "x-quarter-key: try-approver-b7f88ef061bd" https://sandbox.quarterfi.com/approvals/<approval_id>/approve`, then send the same call again with the header `x-quarter-approval: <approval_id>`. Each agent may spend $10 a day there, and everyone together $100.

## Contents

- [Install](#install)
- [Try it in two minutes](#try-it-in-two-minutes)
- [Try the hosted sandbox](#try-the-hosted-sandbox-nothing-to-install)
- [What it is](#what-it-is)
- [Run it](#run-it)
- [For individuals](#for-individuals)
- [Try it with a real agent, free](#try-it-with-a-real-agent-free)
- [Trying Quarter without breaking anything](#trying-quarter-without-breaking-anything)
- [Connect mode: book spend you already have](#connect-mode-book-spend-you-already-have)
- [Keep credentials away from agents](#keep-credentials-away-from-agents)
- [Finance](#finance)
- [Spend on agents](#spend-on-agents)
- [Forecast](#forecast)
- [Receipts and alerts](#receipts-and-alerts)
- [Why a task cost that](#why-a-task-cost-that)
- [Runaway agents and cheaper models](#runaway-agents-and-cheaper-models)
- [The Quarter feed: prices and rules kept current](#the-quarter-feed-prices-and-rules-kept-current)
- [Evidence: tamper-evident ledger and audit pack](#evidence-tamper-evident-ledger-and-audit-pack)
- [One policy for every way agents pay](#one-policy-for-every-way-agents-pay)
  - [Agent wallets: checked on chain, and refusing on their own](#agent-wallets-checked-on-chain-and-refusing-on-their-own)
  - [Agent cards on Mercury and Ramp](#agent-cards-on-mercury-and-ramp)
  - [Spend mandates: proof for the seller](#spend-mandates-proof-for-the-seller)
  - [Bill pay from your own account](#bill-pay-from-your-own-account)
  - [Quarter cards on Stripe Issuing](#quarter-cards-on-stripe-issuing)
- [Change log](#change-log)
- [Agent register and quarterly review](#agent-register-and-quarterly-review)
- [Teams and roles](#teams-and-roles)
- [Backup and restore](#backup-and-restore)
- [If the ledger is down](#if-the-ledger-is-down)
- [Configuration](#configuration)
- [How costs are counted](#how-costs-are-counted)
- [Not built yet](#not-built-yet)

## What it is

The finance layer for AI agents. Agents call this gateway instead of the vendor. It works in five steps:

1. Holds the worst-case cost of the call.
2. Checks it against the caps.
3. Forwards the call, with the agent's own vendor key (never stored).
4. Records the real cost per agent, task and customer.
5. Gives finance a dashboard, approvals and a month-end journal.

Or keep the gateway you run: connect mode books the spend it already records, with no traffic rerouted (see Connect mode).

Details and decisions live in `quarter/memory`: D87 and `strategy/agent-spend-company-plan-2026-10-30.md`.

## Run it

```bash
uv sync
uv run quarter-ledger init     # writes .env (fresh keys) and caps.json ($100 a month in total)
# put your vendor keys in .env: OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY
uv run --env-file .env quarter-ledger serve
```

Point an agent at the gateway and give it the agent key from `.env` as its API key:

```python
client = OpenAI(base_url="http://127.0.0.1:8780/openai/v1", api_key="qa_...")
```

No vendor key yet? `python3 -m quarter_ledger.mock_upstream` (or `uv run python -m quarter_ledger.mock_upstream` in a checkout) answers OpenAI and Anthropic chat calls on port 8790 with a canned reply and real-looking usage, for free. Put `{"openai": "http://127.0.0.1:8790", "anthropic": "http://127.0.0.1:8790"}` in `upstreams.json`, any `OPENAI_API_KEY` value in `.env`, and add `--upstreams upstreams.json` to `serve`.

The gateway sees its own key, swaps in the vendor key from its environment, and records the call. `serve` with no flags and no environment also runs, on localhost, with no caps and agents sending their own vendor keys.

Claude Code: `uv run --env-file .env quarter-ledger wrap -- claude` starts it pointed at the gateway with the agent key, as agent `claude-code`. If `.env` holds `ANTHROPIC_API_KEY`, Claude Code bills that key instead of your subscription. Signed in with Claude Pro or Max, calls stay on the subscription: Quarter applies loop stops and approvals, records them at no cost, and answers `x-quarter-billing: subscription`. `wrap` also turns on Claude Code's subagent and prompt headers, so each subagent's calls show apart. To make Quarter the only place company machines may send Claude Code, `uv run --env-file .env quarter-ledger managed-settings --url <gateway address>` prints the managed settings file and where it goes on macOS, Linux and Windows (or push it by MDM). Claude Code v2.1.285 or later then refuses any other destination, and no user, project or shell setting overrides it; a claude.ai login keeps working. Every `anthropic-*` and `openai-*` header is forwarded unchanged, so new Claude Code features keep working.

Codex: `uv run --env-file .env quarter-ledger managed-settings --for codex --url <gateway address>` prints Codex's `requirements.toml` and where it goes (`/etc/codex/requirements.toml`, `%ProgramData%\OpenAI\Codex\requirements.toml`, or MDM). No user, project or session setting overrides it. It follows OpenAI's gateway guide: a ChatGPT sign-in stays the credential and Quarter forwards it to ChatGPT's Codex backend, so plans keep working. Quarter records each call at no cost with its worth at OpenAI's list price, and applies loop stops, pauses and tool-call holds.

Run the other commands below from the same folder (the ledger is `ledger.db` there) and with `--env-file .env` too, so that `forecast` and the rest see your caps and keys.

**Base URLs:** prices and upstreams for these are built in.
- OpenAI: `http://127.0.0.1:8780/openai/v1`
- Anthropic (and Claude Code): `http://127.0.0.1:8780/anthropic`
- Gemini, OpenAI-compatible: `http://127.0.0.1:8780/gemini/v1beta/openai`
- OpenRouter: `http://127.0.0.1:8780/openrouter/api/v1`
- Any other vendor or API: add it to `upstreams.json` as `/{vendor}/...`, and start `serve` with `--upstreams upstreams.json` (or set `QUARTER_UPSTREAMS`). The file is not read on its own. GET, POST, PUT, PATCH and DELETE all work, query strings included. Streamed responses pass straight through, except tool calls a rule holds. An API with no price is not charged; see Keep credentials away from agents.

**Keys:**
- `QUARTER_AGENT_KEYS` is a comma-separated list. An item written `researcher=qa_...` names that key's agent.
- An agent key works as the vendor key (`Authorization: Bearer` or `x-api-key`) or as `x-quarter-key`. Sent as the vendor key, it is replaced with the gateway's key for that vendor (`QUARTER_VENDOR_KEYS='{"vendor": "key"}'` for others, or `vendor:agent` for one agent's own key). Otherwise the agent's own vendor key goes through untouched. Vendor keys are never stored.

**Optional headers:**
- `x-quarter-agent`: otherwise the key's name, otherwise `default`. A named key speaks only for its own agent: another name is refused with 403, so an agent cannot rename itself out of a pause or a cap.
- `x-quarter-task`: otherwise Claude Code's session id, when it sends one.
- `x-quarter-customer`.

**Answers that never reach the vendor:** each is in the vendor's own error shape, so the OpenAI and Anthropic SDKs raise a typed error whose message says what happened and what to do. Quarter's code is `error.code` (`cap_reached`, `approval_required` and the rest below), and details such as `approval_id` and `reason` are in the error object and beside it. Pointless SDK retries are switched off with `x-should-retry: false`. `uv run --with openai --with anthropic python tools/sdk-check.py` checks all this with the official SDKs against a local mock.
- **402:** the call is over a cap. A cap with `"over": "approve"` sends it to a person instead (403 below): approved, that one call goes past the cap.
- **403 rule_denied:** a rule in `QUARTER_RULES` forbids the call.
- **423 agent_paused:** the agent is paused.
- **403:** the call needs approval. The reply carries an `approval_id`. Once finance approves, retry once with `x-quarter-approval: <id>`. Or send `x-quarter-wait: <seconds>` (at most 300, or `QUARTER_MAX_WAIT`) and the gateway holds the call open until someone decides: approved, it goes on to the vendor; denied, it gets `403 approval_denied` with `decided_by`; no answer in time, the usual `approval_required`.

**API description:** `GET /openapi.json` (no key) is the OpenAPI 3.1 description of Quarter's own routes: `/authorize`, `/settle`, approvals, finance and the vendor passthrough.

Hosting it: `docs/DEPLOY.md`, or `docs/HOSTED.md` for many customers on one box. The image takes all config from the environment (`QUARTER_CAPS`, `QUARTER_PRICES`, `QUARTER_UPSTREAMS` as JSON or a path, `QUARTER_DB`, `HOST`, `PORT`). In a `.env` read by `uv run --env-file`, put JSON values in single quotes (`QUARTER_CAPS='[...]'`); in Docker's `--env-file`, leave them unquoted.

## For individuals

`uv run --env-file .env quarter-ledger status` shows, in 80 columns: spend today and this month, each cap with a bar, the month-end forecast and the day a cap will be reached, approvals waiting and paused agents. Days and months are UTC.

Your agents can ask the same ledger themselves. `quarter-ledger mcp` is a read-only MCP server on stdio with four tools:
- `budget_left`: for an agent (and optionally a task or customer), each cap that applies, what is left, and whether the agent is paused.
- `spend`: today or this month, by agent, task or customer.
- `approval_status`: pending, approved or denied, for the `apr_...` id a 403 returned.
- `why`: where one task's money went (see below).

No tool approves, changes a cap or spends. Add it to Claude Code with your folder's absolute path:

```bash
claude mcp add quarter -- uv run --directory /path/to/ledger quarter-ledger mcp --caps /path/to/ledger/caps.json
```

It reads `ledger.db` in that folder. Add `--scope user` before `quarter` to have it in every project.

## Try it with a real agent, free

`examples/agent_demo.py` is a small agent with two tools that plans an offsite, sending every model call through Quarter. Run any OpenAI-compatible model locally, for example `llama-server -m <model>.gguf --port 8794 --jinja`, then:

```bash
QUARTER_AGENT_KEYS="planner=<a long key>" QUARTER_UPSTREAMS='{"local":"http://127.0.0.1:8794"}' \
QUARTER_VENDOR_KEYS='{"local":"none"}' QUARTER_PRICES='{"local":{"*":{"input_per_million":"50","output_per_million":"150"}}}' \
QUARTER_CAPS='[{"scope":"agent","key":"planner","window":"month","limit_usd":"0.15","approve_above_usd":"0.05"}]' \
uv run quarter-ledger serve
python examples/agent_demo.py --gateway http://127.0.0.1:8780/local/v1 --key <the key> --wait 60
```

The made-up price makes spend visible. The agent waits for an approval (`uv run quarter-ledger approvals`, then `approve <id> --as <you>`), and is refused once the budget is spent. Tested on 2026-11-02 with Qwen 2.5 1.5B.

## Trying Quarter without breaking anything

Agents that already work should keep working on the day Quarter goes in. Shadow mode lets every call through and records what Quarter would have done.

- **Everything in shadow:** set `QUARTER_MODE=shadow`. Caps, payee lists, pauses and loop stops then hold nothing. Each call one of them would have refused or sent to a person goes through, and the audit log gets a `would_hold` row with the rule, the amount and who would have been asked. Spend is held and charged exactly as before, so the ledger stays complete.
- **One rule at a time:** add `"mode": "shadow"` to a cap in `caps.json`, to `QUARTER_PAYEES`, or to `QUARTER_RUNAWAY`. Only that rule goes quiet; every other rule still refuses. Call rules and tool rules (`QUARTER_RULES`) and the bill checks always enforce.
- **What the agent sees:** a call shadow let through carries `x-quarter-shadow: would_block` or `would_approve`. `POST /authorize` still returns the authorisation and the mandate, with `"shadow": {"would": ..., "reason": ...}` beside them.
- **Nobody can miss it:** `serve` prints a `SHADOW MODE` line for each rule in shadow, and the dashboard shows a banner with the count this month. The setting is part of the config, so turning it on or off is in the change log.
- **What it would have held:** `uv run quarter-ledger shadow-report --since 2026-11-01` lists each rule with how many calls it would have held, the money involved and the people who would have been asked. It also says how many of those asks a standing approval would have answered, one in force or one `suggest` offers. `--csv` writes it as a file.
- **Testing a rule change first:** `uv run --env-file .env quarter-ledger replay --caps new_caps.json [--payees new_payees.json] [--days 30]` runs the proposed rules over the calls recorded in the last 30 days, in order, through the gateway's own decision code. Each call sees the windows as they were when it was made. It reports the calls the new rules would newly hold, the ones they would no longer hold, and the money in each. It only reads the ledger.
  - It first checks itself: the live rules (`QUARTER_CAPS` and `QUARTER_PAYEES`) should give back the decision each call really got. A rule changed during the period, or a call moved to a cheaper model, explains a difference.
  - Windows hold what was recorded, so a call the new rules would refuse still counts toward later windows. Pauses, bills and calls to APIs that are not models are left out.
  - A refused call is in the audit log as `refused`, so replay sees calls that never reached the vendor too.

## Connect mode: book spend you already have

Quarter can work with no traffic rerouted. Keep LiteLLM, Cloudflare AI Gateway, OpenRouter or the vendor keys you use today. `connect` reads the spend those already record and books it in the same ledger. Every report then works on it.

```bash
uv run --env-file .env quarter-ledger connect litellm --month 2026-10
uv run --env-file .env quarter-ledger connect openai --month 2026-10 --map map.json
```

**What it books:**
- Each piece of spend is an **observed** entry: a charge in the same hash-chained ledger, dated when the spend happened, with a call id starting `obs:<source>:`. It has no hold and no approval behind it, and no approval counts it.
- `status`, `report`, `summary`, `export` (with `--cost-centres` and `--unbilled`), `margin`, `forecast`, `agents list`, the dashboard and the audit pack all include it. `status` shows it under each cap as `+ $X observed, alert only`. `why <task>` gives a task's calls and cost, but not the parts or cache breaks: those need the prompt, which only the gateway sees. `reconcile` leaves out what `connect` booked from that vendor's own cost API, so it still shows what the gateway and the other sources missed.
- No gateway decision counts it. Caps, standing approvals, loop stops and the register (`QUARTER_REGISTER`) count only what passed through the gateway, so observed spend never blocks, asks or pauses a call. The gateway still fails closed exactly as before.
- Each pull is an `observed_pull` row in the audit log, with the source, the period and what it booked.

**Safe to run again:**
- Every record is kept by its source id. Running the same period again books nothing twice.
- A day's total from a vendor can still change after it is read. Reading it again books only the difference, as a new entry; nothing is edited.
- Each page is written on its own, so a pull cut short keeps what it booked. With no `--month` or `--since`, a pull starts where the last one stopped, less a day for late records. Run it daily from cron.
- It only reads. Each key goes only to its own host, is never printed, and is never sent on a redirect.

**Two sources, one bill:** the vendor cost APIs and OpenRouter activity give totals that include spend a detailed source (LiteLLM, Cloudflare, OpenRouter generations, a CSV) or the gateway may have booked already. So a total books only the rest: the vendor's figure for that day and model, less what the ledger already holds for it, never below zero. Pull the detailed sources first. Reading a total again after them corrects it.

**Alerts, never blocks:** after each pull, Quarter posts to Slack (if set up) or the approval webhook, once each:
- **Spend jump:** an agent or customer whose observed spend on one day passed $20 and three times its daily average over the 7 days before. Change them with `--spike-floor-usd` and `--spike-multiple`. A new agent or customer has no average, so only the floor counts for it. Days older than 3 days are not checked, so a backfill raises no old news.
- **Budget:** a named monthly cap in `--caps` (or `QUARTER_CAPS`) reached, or on course to be, counting observed spend. It says the spend is not blocked.
- The audit log remembers each alert (`spike_alert`, `budget_alert`), so a re-run sends nothing twice. `--no-alerts` books only.

**Who spent it:** `--map` (JSON text or a path) says which fields name the agent, the customer and the task, each a list tried in order. A field is a dotted path (`metadata.customer_id`); one ending in `[prefix]` takes a list entry that starts with it (`request_tags[customer:]` finds `customer:acme` and gives `acme`). Fields that hold JSON text are read as JSON. The defaults are below.

**The sources:**
- **`openai`, `anthropic`:** the cost APIs `reconcile` reads, with the same `OPENAI_ADMIN_KEY` or `ANTHROPIC_ADMIN_KEY`, per UTC day. Only finished days are read. `{"projects": {"proj_...": "support-bot"}}` (OpenAI) or `{"workspaces": {"wrkspc_...": "claude-code"}}` (Anthropic) groups the costs by project or workspace and books each as that agent. The rest of a day's total is split over them in the bill's own proportions. The vendors have no customer field.
- **`litellm`:** a LiteLLM proxy's `GET /spend/logs/v2` (`page`, `page_size` 1000) with `QUARTER_LITELLM_URL` and `QUARTER_LITELLM_KEY` (a key that may read spend logs), one day at a time. Or `--file` with a CSV or JSON export of the same rows. Agent: `metadata.user_api_key_alias`, `user`, `metadata.user_api_key_team_alias`, `team_id`. Customer: `end_user`, `request_tags[customer:]`. Task: `session_id`, `request_tags[task:]`. The vendor is `custom_llm_provider`, else the model's prefix (`anthropic/...`).
- **`openrouter`:** `GET /api/v1/activity?date=` with a management key in `QUARTER_OPENROUTER_MANAGEMENT_KEY`: daily totals per model, for the last 30 finished days only, so pull at least monthly. `{"keys": {"<sha256 of a key>": "researcher"}}` reads each key's day too (`api_key_hash`) and books it as that agent. Activity has no customer. It books `usage`, what OpenRouter charged; spend on your own provider keys (BYOK) is billed by that provider, so pull it from there.
- **`openrouter-gen`:** for a customer split on OpenRouter. `--file` is a CSV your app logged: the `id` of each generation (`gen-...`) and, if you have them, `agent`, `customer` and `task`. Each id not booked yet is read once from `GET /api/v1/generation` with `OPENROUTER_API_KEY` (or `QUARTER_OPENROUTER_KEY`). Customer falls back to `external_user`, task to `session_id`. Use this or `openrouter` for the same spend; if both, pull this first.
- **`cloudflare`:** an AI Gateway's `GET /client/v4/accounts/{account}/ai-gateway/gateways/{gateway}/logs` (`per_page` 50) with `QUARTER_CLOUDFLARE_TOKEN` (AI Gateway Read), `QUARTER_CLOUDFLARE_ACCOUNT` and `QUARTER_CLOUDFLARE_GATEWAY`, one day at a time. Send `cf-aig-metadata` on your calls (`{"agent": ..., "customer_id": ..., "task": ...}`). Agent: `metadata.agent`, `metadata.user`. Customer: `metadata.customer`, `metadata.customer_id`. Task: `metadata.task`, `metadata.session_id`. The vendor is the log's `provider`; the cost is Cloudflare's own figure.
- **`csv`:** anything else. `--file` with a date (`date`, `timestamp`, `time`, `at`, `created_at`, `start_time`) and an amount in dollars (`amount_usd`, `cost_usd`, `amount`, `cost`, `spend`), plus any of `id`, `vendor` (or `provider`; else `--vendor`), `model`, `agent`, `customer` (or `customer_id`), `task`, `input_tokens`, `output_tokens`. Name your own headers with `{"columns": {"at": "When", "amount": "Cost ($)"}}`. With no id column, a row's id is a hash of its values, so the same file twice books once. Negative amounts are refused: record refunds apart. `--name` keeps two kinds of file apart.

**What could not be confirmed** (read from each vendor's documentation on 2026-11-03; none of it has been called with a real key, and tests run against recorded responses built from the documented shapes):
- OpenAI: that `group_by` takes `line_item` and `project_id` together as a repeated parameter, and the result's `project_id` field. Everything `reconcile` notes about amounts in dollars applies.
- Anthropic: that `cost_report` groups by `workspace_id` together with `description`, and the result's `workspace_id` field.
- LiteLLM: `/spend/logs/v2` and its `page` and `page_size` are documented, its reply is not. Quarter reads `data` (or a bare list) and `total_pages`. The row fields (`request_id`, `spend`, `startTime`, `model`, `user`, `team_id`, `end_user`, `request_tags`, `session_id`, `custom_llm_provider`, `prompt_tokens`, `completion_tokens`) follow LiteLLM's spend log table, not a documented reply. Whether `end_date` is inclusive is not said; Quarter asks a day past and keeps rows by `startTime`. The UI's CSV export was not found documented, so `--file` expects the same column names.
- OpenRouter: activity and generation fields are as documented. That activity can be read per key with `api_key_hash` is documented; that it then returns only that key's spend was not tested.
- Cloudflare: `start_date` and `end_date` are marked deprecated in favour of `filters`; Quarter still sends them, and also keeps only logs inside the day. `metadata` is documented as a string; Quarter reads it as JSON text, or as an object.

## Keep credentials away from agents

An agent should never hold a real key for the APIs it acts on. Give it only its Quarter key. Quarter puts the real key on the call on the way out, so the agent cannot print it, leak it or use it anywhere else. Destructive calls can wait for a person.

**1. Add the APIs as upstreams** in `upstreams.json`:

```json
{"railway": "https://backboard.railway.com", "github": "https://api.github.com"}
```

**2. Give the gateway the real keys,** one per upstream or one per agent:

```bash
QUARTER_AGENT_KEYS="deployer=qa_...,reviewer=qa_..."
QUARTER_VENDOR_KEYS="railway=<team token>,railway:deployer=<deployer token>,github=<read-only token>"
```

`railway:deployer` is the deployer's own key for Railway. Other agents get the `railway` key. Only a named agent key gets its own key, because an unnamed key can claim any agent name. The JSON form (`{"railway:deployer": "..."}`) works too.

**3. Point the agent at the gateway,** with its Quarter key where the real key would go:

```bash
curl http://127.0.0.1:8780/railway/graphql/v2 -H "Authorization: Bearer qa_..." \
  -H "content-type: application/json" -d '{"query": "query { me { name } }"}'
```

**4. Write the rules** in a file named by `QUARTER_RULES`:

```json
[
  {"upstream": "railway", "default": "deny"},
  {"upstream": "railway", "method": ["POST"], "path": "/graphql/v2", "graphql_operation": "*Delete", "action": "approve"},
  {"upstream": "railway", "method": ["POST"], "path": "/graphql/v2", "action": "allow"},
  {"upstream": "github", "method": ["DELETE"], "path": "/repos/*", "action": "deny"},
  {"agent": "reviewer", "upstream": "github", "method": ["POST", "PUT", "PATCH"], "action": "deny"},
  {"tool": "Bash", "command": "rm\\s+-rf\\s+/(\\*|\\s|$)|drop\\s+table|terraform\\s+destroy", "action": "approve"},
  {"tool": "Bash", "command": "git\\s+push(?=.*(--force|\\s-f\\b))(?=.*\\bmain\\b)", "action": "approve"},
  {"tool": "mcp__*__delete_*", "action": "approve"}
]
```

This file does three things:
- **Railway:** anything but a POST to `/graphql/v2` is denied. A mutation ending in `Delete`, such as `volumeDelete`, waits for a person. Other GraphQL calls go through.
- **GitHub:** deletes under `/repos/` are denied for every agent. The reviewer may only read. The rest goes through, since GitHub has no default.
- **Model replies:** a Bash call that deletes from the root, drops a table, destroys Terraform state or force-pushes to main waits for a person. So does any MCP tool named `delete_*`.

**How call rules work:**
- A rule has `agent` (`*` or a name), `upstream`, `method` (`*` or a list), `path` (a glob) and `action`: `allow`, `deny` or `approve`. `graphql_operation` is optional. `agent`, `method` and `path` default to `*`.
- The first rule that matches wins. An upstream with no rules allows everything, as before. An upstream marked `"default": "deny"` denies whatever no rule allows.
- The path is matched after decoding, and globs ignore case, so `%2F` or capitals cannot dodge a rule. `*` matches across `/`.
- **GraphQL:** Quarter reads the query and checks each field the operation runs at its root, aliases and fragments included. The call gets the strictest answer. `operationName` is a label the client picks, so it counts only when no query is sent. A GraphQL call Quarter cannot read (a multipart upload, say) waits for a person, unless a rule denies it.
- `deny` answers `403 rule_denied` with the rule. `approve` uses the usual approval: `403 approval_required` with an id, or `x-quarter-wait` to hold the call open, with Slack buttons. Approved, the call goes on. An approval covers one call: that agent, that method and path, those GraphQL fields.

**How tool rules work:**
- A tool rule has `tool` (a glob on the tool name), `command` (optional: a regular expression, ignoring case, searched in every string of the tool's input) and `action` (`approve` or `deny`).
- They apply to replies from Anthropic Messages, OpenAI Chat Completions and OpenAI Responses, streamed or not. A stream passes through as it arrives until a tool call starts; the rest is kept back until the reply ends, then checked. Held calls are taken out and a note takes their place, with blocks renumbered so the SDK still reads the stream. So text reaches the agent at once, and tool calls only once checked. While a person decides (`x-quarter-wait`), the stream pauses.
- Approved, the agent gets the reply as the model sent it. Denied, or not decided in time, the agent gets the reply without those tool calls and a note saying why. A `deny` rule takes them out without asking.
- With no `x-quarter-wait`, the note carries the approval id. Once it is approved, send the same request with `x-quarter-approval: <id>`. It goes through if the model asks for the same tool call again.
- The approval shows the first 120 characters of the tool's input, so the person can decide. Anything that looks like a key or password is replaced with `[hidden]` first; a password glued to a flag (`-pS3cret`) is not caught. The text is kept with the approval, so it reaches Slack, the dashboard and the audit pack. The model call is charged either way.

**Routes, for where data may go:**
- A route sends calls to another upstream instead of refusing them: `{"upstream": "openai", "customer": "eu-*", "to": "openai-eu"}`, with `openai-eu` in `upstreams.json` as the vendor's EU endpoint (for OpenAI's EU data residency, `https://eu.api.openai.com`). `agent` and `customer` are globs and default to `*`; the first route that matches wins.
- Rules and prices still go by the upstream the agent named. The routed call uses only the key set for `to` (`QUARTER_VENDOR_KEYS`), never the vendor's, so no key reaches a host it was not made for. The reply carries `x-quarter-upstream`, and the ledger records where each call went (`upstream` on the call, `routed_to` on an action), which is the evidence a residency rule asks for.

**Redaction, for what data may go:**
- A redaction masks values before a model sees them and puts them back in the reply: `{"redact": ["card", "email"], "customer": "eu-*"}`. The kinds are `email`, `card` (passing the Luhn check), `iban` (passing its check digits), `ssn` (US, as 123-45-6789) and `phone` (international, starting with +). Add your own with `"patterns": {"patient": "P\\d{8}"}`. `upstream`, `agent` and `customer` are globs and default to `*`; every redaction that matches applies.
- The vendor gets a placeholder such as `[EMAIL_3f2a9c1b]`, the kind and a keyed hash of the value. The same value gets the same placeholder in every call, so the prompt cache still hits, and the vendor cannot work the value back out. The agent gets its reply with the real values, in text and in tool calls, streamed or not. So a model can still write "email ana@example.com the receipt" and call the tool, without ever seeing the address.
- It applies to model calls and token counts, not to calls on APIs agents act on, which need the real values. A held tool call is shown to a person with the placeholders, so the values do not reach Slack either. Images, files and other base64 data are left as they are, and so is the model's thinking, which keeps its placeholders: the vendor checks it comes back unchanged.
- The reply carries `x-quarter-redacted: card:1,email:2`, and the ledger keeps the same on the call (`redacted`), never the values. That is the record a data protection rule asks for.
- A false match, such as an order number that passes the Luhn check, costs little: the model sees a placeholder and the agent gets the number back. The placeholders are keyed from the gateway's signing key, so they stay the same across restarts.

**What is recorded:**
- A call to an upstream with no price, or a PUT, PATCH or DELETE with no `model`, is not held or charged. The audit log gets an `upstream_call` row: agent, task, upstream, method, path, decision and who decided. Never a body, a query string or a key.
- A held tool call is a `tool_held` row: the tool names and the decision, never the input.
- A paused agent gets `423` on every upstream.
- Every method keeps the same checks: the agent key, the named key's agent, the body limit, no chunked bodies, no `..` in the decoded path, and no redirects followed.
- Rules load at start. A typo in a key, an action or an upstream name stops the gateway from starting. Changes show in the change log. `status` lists the rules, and the audit pack holds `rules.json` and an Egress control.

## Finance

- **Dashboard:** `http://127.0.0.1:8780/dashboard`. The password is the admin key or your own key from QUARTER_PEOPLE (see Teams and roles), and any username works. It shows a month's spend, caps used, and approvals with Approve and Deny buttons. Pick the month at the top (`?month=2026-10`); in the first 7 days of a month it opens on last month, for the close. Caps and the forecast show for this month only; approvals and paused agents always show.
- **Close files on the dashboard (finance or admin):** buttons download the month's journal (CSV, QuickBooks, Xero or NetSuite), FOCUS rows, the audit pack and the approvals record. They are the files `export`, `audit-pack` and `approvals export` write, from `GET /export?month=&format=`, `GET /audit-pack?month=` and `GET /approvals/export?month=`. Each download is audited as `download` with who took it. The dashboard's journals split by the cost centres below; the audit pack's journal does not, as with the CLI.
- **Cost centres on the dashboard (finance or admin):** a form maps each agent to a department (the team), class and location, the same map `export --cost-centres` reads. It is saved to `QUARTER_COST_CENTRES` (default `ledger.db.centres.json` next to the ledger). Each change is an audit row, `cost_centre`, such as `department claude-code: (none) -> Engineering`, with who made it. If someone saved in between, the form is refused (409) and asks for a reload.
- **Approvals in Slack:** set `QUARTER_SLACK_BOT_TOKEN` (a bot token with `chat:write`), `QUARTER_SLACK_SIGNING_SECRET`, `QUARTER_SLACK_CHANNEL` and `QUARTER_SLACK_USERS='{"U012ABC": "Sam"}'`. Each approval is posted to the channel with Approve and Deny buttons, in place of the webhook message. Point the Slack app's Interactivity request URL at `https://<gateway>/slack/actions`. Clicks are checked against Slack's signature and refused when older than 5 minutes; only people in `QUARTER_SLACK_USERS` can decide, and the ledger records their name as the approver. The owner rule below applies. The message then shows who decided.
- **Approvals from the terminal:** `uv run quarter-ledger approvals`, `approve <id>`, `deny <id>`.
- **Learning from approvals:** `uv run quarter-ledger suggest` (or `GET /approvals/suggestions`, and the dashboard) reads the last 90 days of decisions. Where people approved at least 5 requests from one agent to one vendor or payee and denied none, it suggests a standing approval sized to them: the largest amount approved and the busiest month of all that agent's spend with that vendor (a standing approval's monthly limit counts every call, not only the approved ones), rounded up to whole dollars. Where people denied at least 3 and approved none, it says so, so a rule or payee list can refuse them first. It also shows the median wait for a decision. Nothing changes until a person accepts.
- **Standing approvals:** `uv run quarter-ledger standing add --agent researcher --vendor exa --up-to-usd 5 --monthly-usd 19 --by <you>` (or `POST /standing`, approver role). Calls from that agent to that vendor up to that amount, and within that monthly total, skip the approval line. Caps still apply: going past one still blocks or asks. A first payment to a bill's account, changed bank details, a possible duplicate invoice and a payee not on the list are always asked. Each one ends (90 days unless `--days`, at most 366), each use is in the audit log as `standing_used`, and the agent's owner cannot grant one. `standing list`, `standing revoke <id> --by <you>` (or `POST /standing/<id>/revoke`).
- **Spend report:** `uv run quarter-ledger report --by customer|agent|task|vendor [--csv]`.
- **Month-end journal:** `uv run quarter-ledger export --month 2026-10 > journal.csv`. It writes one line per customer and vendor plus a balancing accrual, rounded so it always balances. Spend that served a customer posts to `Cost of revenue: AI` (gross margin); spend with no customer posts to `AI vendor costs`. Rename either with `--revenue-cost-account` and `--expense-account`.
- **Into the books as it is:** `--format quickbooks`, `xero` or `netsuite` lays the journal out for that system's own import (QuickBooks Online: Settings, Import data, Journal entries; Xero: Manual journals, Import; NetSuite: Import CSV records, Journal entry). The accounts must already exist there under the names you give; in QuickBooks a colon means parent and sub-account, so the default `Cost of revenue: AI` needs both, or pass your own names. Xero takes account codes, so pass them (`--expense-account 6100` and so on), plus `--tax-rate` with your region's no-tax rate (default `Tax Exempt`). Dates are MM/DD/YYYY; `--date-format` changes that. The customer goes in each line's description; `--customer-names` puts it in the Name column instead, which must then match a customer in that system. QuickBooks takes at most 999 lines in a file and Xero 300; the command warns past that. The QuickBooks and NetSuite headers follow their help pages; the Xero headers follow its import template and are not yet checked against a live import.
- **Each team's AI cost on its own budget line:** `--cost-centres centres.json` names the department, class or location each agent's spend belongs to, and splits the expense lines by them (still one per customer and vendor within each). The file is `{"department": {"claude-code": "Engineering", "support-bot": "Support", "*": "Other"}, "class": {"claude-code": "R&D"}}`; use any of `department`, `class` and `location`. `"*"` takes every agent not named, and costs recorded with no agent. An agent with no entry and no `"*"` is left unassigned, with the column blank. Each split line is rounded on its own and the accrual is their sum, so the journal still balances; the total can differ by a cent or two from the unsplit journal. Without `--cost-centres` the file is exactly as before. The centres must already exist in the target system under the same names.
  - csv: `Department`, `Class` and `Location` columns.
  - NetSuite: `Department`, `Class` and `Location`, the names in its journal entry import field tables ([header fields](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1534492479.html), [line fields](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1534492737.html), and the [sample file](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_1534489651.html) has a `Department` column). Location is a line field. Department and Class are header fields unless Always Allow Per-line Classifications on Journals is on (Setup, Accounting, Accounting Preferences); turn it on, or every line takes one department and class.
  - QuickBooks Online: `Class` and `Location`. Intuit's [import help page](https://quickbooks.intuit.com/learn-support/en-us/help-article/import-export-data-files/import-journal-entries-quickbooks-online/L4tQBwbs7_US_en_US) says class and location tracking must be on to import them, but names no column headers, so these two are not verified; the import lets you map each column if they differ. QuickBooks tracks departments as locations ([location tracking](https://quickbooks.intuit.com/learn-support/en-us/getting-started/how-to-set-up-and-use-location-tracking/00/263128), Plus and Advanced only), so use `location` for them; `department` with `--format quickbooks` is refused.
  - Xero: tracking categories, two at most, as `TrackingName1`, `TrackingOption1`, `TrackingName2` and `TrackingOption2`. The name is the key with a capital (`Department`), and must match the tracking category in Xero exactly. Xero's [product ideas forum](https://productideas.xero.com/forums/967133-reports-tax/suggestions/49162859-manual-journal-import-with-tracking) confirms tracking imports on manual journals, but its help pages could not be read to confirm these column names, so they are not verified. Check them against the template on Xero's import screen.
- **Only what the vendor has not billed yet:** `uv run quarter-ledger export --month 2026-10 --unbilled --billed openai=openai-bill.csv --format quickbooks`. Use this instead of the full journal when vendor bills post to expense through accounts payable, so a month closed before the bills arrive still carries its cost. Each `--billed` file is the vendor's bill for the month, in the CSV shape `reconcile --file` reads. For each vendor, what Quarter charged past the billed total is the accrual, split over that vendor's customer lines in proportion. A vendor with no `--billed` file has billed nothing, so its whole month is accrued. A vendor that billed more than Quarter saw accrues nothing; `reconcile` shows where the rest came from. Spend that served a customer debits `Cost of revenue: AI`, the rest `AI vendor costs`, and the total credits `Accrued liabilities` (`--accrued-account`). The same file holds the reversal, dated the first day of the next month, with every debit and credit swapped. The two entries are numbered `AGENT-ACCR-2026-10` and `AGENT-REV-2026-10`. Seats and subscriptions recorded with `cost add` were paid already and are not accrued. `--cost-centres` and the other export options work the same way.
- **FOCUS for FinOps tools and warehouses:** `uv run quarter-ledger export --format focus --month 2026-10 --csv focus.csv`, or `--from 2026-10-01 --to 2026-11-01` (the end is left out). With no dates it writes every charge. Each row is one charge in [FOCUS 1.4](https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/tree/v1.4), the FinOps Foundation's cost and usage format, so Vantage, CloudZero, Finout or a warehouse table read it as it is. Gateway and observed charges both appear, each once. Holds and releases are not cost and are left out.
  - The columns: all 21 FOCUS 1.4 Mandatory columns in the spec's order, then `ChargeFrequency`, `ConsumedQuantity`, `ConsumedUnit`, `ServiceSubcategory`, `SkuId`, `SkuPriceId` and `Tags`, then Quarter's own: `x_CallId`, `x_Agent`, `x_Task`, `x_Customer`, `x_Governed` (`true` through the gateway, `false` observed by `connect`), `x_InputTokens`, `x_OutputTokens`, `x_CacheReadTokens` and `x_CacheWriteTokens`. FOCUS names custom columns with `x_`.
  - How it maps: the vendor is the service provider, host provider, invoice issuer and service name. The model is the SKU. Cost is in USD with six decimals, exact to the micro-dollar. `BilledCost`, `EffectiveCost` and `ContractedCost` are what Quarter recorded. `ListCost` is the list price for gateway calls, else the same. The charge period is the UTC day, the billing period the calendar month. `PricingQuantity` and `ConsumedQuantity` are all the call's tokens, unit `Tokens`. Agent, customer and task also go in `Tags`. `--account openai=org-abc` sets the vendor's billing account id; without it the vendor name stands in for it.
  - Left null, as an empty cell: `ChargeClass`, because Quarter does not know when a vendor closes a billing period. A vendor total revised down is a negative `Usage` row on its own day. Quantities and units are null where Quarter has no tokens, as for a vendor's daily total; FOCUS wants them on every usage row, so a strict validator flags those rows. `SkuId` and `SkuPriceId` are null for calls with no model, such as a search API, and those rows are `Other`. FOCUS's region, resource, commitment, invoice and unit price columns are not written: Quarter does not have them.
  - Merging with a vendor's own FOCUS data: FOCUS says only the invoice issuer should put cost in `BilledCost`. Quarter is not the issuer, so a table that also loads the vendor's FOCUS export (Azure OpenAI, Bedrock) counts that spend twice. Use one or the other per vendor. Seats and subscriptions recorded with `cost add` are not in the file.
- **Margin after AI cost, by customer:** `uv run quarter-ledger margin --month 2026-10 --revenue revenue.csv`. The revenue file is your billing export for that month: a customer column (`customer`, `customer_id`, `customer name`) and an amount column (`amount_usd`, `amount`, `revenue`, `amount paid`, `total`). Customer names must match the ones your agents send. It lists each customer's revenue, AI cost (agent spend plus costs recorded for that customer) and margin, worst first, and flags customers who cost more than they pay or have AI cost and no revenue. It also shows how much of the cost the heaviest 5% of customers account for, the median cost per customer, and spend that named no customer. `--csv` writes it as a file. This is margin after AI cost only: hosting, support and the rest of cost of revenue are not in it.
- **Revenue straight from Stripe:** `uv run quarter-ledger margin --month 2026-10 --stripe` reads it instead of a file, with `STRIPE_REVENUE_KEY` set to a restricted key (`rk_...`) that can read invoices and customers and nothing else; Quarter refuses a full secret key. Revenue is invoices paid that month, excluding tax, less credit notes issued after payment; invoices in other currencies are counted and left out. A Stripe customer is booked under its `quarter_customer` metadata when set, so it matches the customer your agents send, else the name on the invoice. With the key set for `serve`, finance's dashboard shows the same margin for the chosen month, reading Stripe at most every 10 minutes.
- **Capitalising coding-agent spend:** `uv run quarter-ledger capitalise --month 2026-10 --projects projects.json --evidence evidence.csv`. Under ASC 350-40 (as amended by ASU 2025-06), costs directly attributable to building internal-use software can be recorded as an asset once the project qualifies, and coding-agent spend can count. Whether a project qualifies, and from when, is your judgement and your auditor's; Quarter supplies the calls behind it. `projects.json` lists each project with `from`, an optional `to`, and the `agents` and task patterns (`tasks`, such as `checkout-*`) that belong to it; each needs agents or tasks. A call matching two projects is counted in neither and reported. The evidence file has one row per project, task, agent, vendor and model, with calls, amount and the first and last call. `--journal quickbooks` (or `csv`, `xero`, `netsuite`) prints the entry that moves that spend from the accounts `export` booked it to into `Capitalised software` (`--asset-account`). Set the task for Claude Code work with `x-quarter-task` in `ANTHROPIC_CUSTOM_HEADERS`. Seats and subscriptions recorded with `cost add` are not included.
- **The vendor's bill against Quarter:** `uv run --env-file .env quarter-ledger reconcile --month 2026-10 --vendor openai` (or `anthropic`). Vendors bill errors, credits go missing, and some spend never passes through the gateway. This compares what the vendor says you spent that month with what Quarter charged to that vendor, per model and in total, largest difference first. Each line shows the vendor's amount, Quarter's, the difference and the difference as a share of the vendor's amount.
  - **Where the vendor's figure comes from:** its own cost API, read with your admin key from `OPENAI_ADMIN_KEY` or `ANTHROPIC_ADMIN_KEY`. The key is sent only to that vendor, is never printed, and a redirect is never followed with it. Or, with no key at all, `--file costs.csv`: a cost export from the vendor's console with a date column (`date`, `day`, `period`, `month`, `start_time`, `starting_at`, `usage date`, as YYYY-MM-DD or YYYY-MM), an optional model or line item column (`model`, `line_item`, `line item`, `description`, `item`) and an amount column in dollars (`amount_usd`, `amount`, `cost_usd`, `cost`, `total`). Rows outside the month are left out. These column names were not checked against a real console export, so rename a header if yours differs.
  - **The flags:** a model the vendor billed and Quarter never saw is spend outside the gateway: a key used directly, the console playground, or another tool. A model Quarter saw and the vendor did not bill is a credit, a model named differently, or usage the vendor has not billed yet. Any other difference over `--tolerance-pct` (default 2) is flagged too. Differences under a cent are not.
  - **Subscriptions:** Claude Code on a claude.ai plan is recorded at $0, so it is in neither column; the command says how many such calls there were and what they were worth at list price. The plan is billed apart from API usage. Codex on a ChatGPT plan is recorded under vendor `chatgpt` and is not compared.
  - `--csv` writes it as a file, with the total as the last row.
  - **Limits:** models are matched by name, ignoring a dated suffix, so `gpt-4o` matches `gpt-4o-2024-08-06`. OpenAI line items are split at their last comma (`gpt-4o-2024-08-06, input` is `gpt-4o-2024-08-06`); line items of other products, such as `assistants api | file search`, stay whole and show as spend outside the gateway. Anthropic costs that are not tokens (web search, code execution) show by description, the same way. The vendor groups by UTC day, and so does Quarter. Calls a routing rule sent to another upstream are still counted under the vendor the agent named. Anthropic leaves Priority Tier costs out of this API. The total is the figure to trust; the per-model lines explain it.
  - **The APIs, as their documentation states them:** OpenAI `GET /v1/organization/costs` with `bucket_width=1d` and `group_by=line_item` ([reference](https://developers.openai.com/api/reference/resources/organization/subresources/audit_logs/methods/get_costs), [cookbook](https://developers.openai.com/cookbook/examples/completions_usage_api)). Anthropic `GET /v1/organizations/cost_report` with `group_by[]=description`, which Anthropic calls legacy but supported, and which reports cents as a decimal string ([guide](https://platform.claude.com/docs/en/manage-claude/usage-cost-api), [reference](https://platform.claude.com/docs/en/api/beta/organization/cost_report/retrieve)). OpenAI's reference does not say in words that its amount is in dollars; its example and cookbook treat it so, and so does Quarter. Neither API has been called with a real key yet: tests run against a local server built from the documented shapes.

## Spend on agents

- **Spend on agents.** This covers seats, subscriptions, compute and people.
  - **Recording a cost:** `uv run quarter-ledger cost add --category seat --vendor cursor --amount-usd 40 --month 2026-10 --monthly --agent coder`, or `POST /costs`.
  - **Monthly costs** count every month until `cost end <id> --last-month YYYY-MM`.
- **Per agent:** `uv run quarter-ledger summary --month 2026-10` shows what each agent spent, what was spent on it and the total. The dashboard and the journal include both.

## Forecast

- **Forecast:** `uv run quarter-ledger forecast`, or `GET /forecast`. Month-to-date spend plus the last seven days' daily rate for the rest of the month, per agent, and the date each named monthly cap would be reached. The seven days reach back into last month, so early in a month the rate is not stretched from a few hours. A ledger younger than seven days uses the time since its first charge; one younger than a day shows "not enough data yet" instead of a number.
- The dashboard shows a month-end card and a forecast table.

## Receipts and alerts

- **Signed approval receipts:** `serve` makes an `ed25519:` key beside the ledger on first run (`ledger.signing-key`, owner-only, never replaced) unless `QUARTER_SIGNING_KEY` is set. Then `uv run quarter-ledger receipt <approval id>` or `GET /approvals/<id>/receipt`. The receipt is a JWT (HS256) saying which agent was allowed to spend up to how much, on what, and who decided. Anyone with the key can check it with `quarter-ledger verify <token>`. With a key written `ed25519:<64 hex seed>` the receipt is signed EdDSA instead, and anyone can check it with only the public key (`ed25519-pub:<hex>`). It follows the idea of an AP2 intent mandate but is not an AP2 credential.
- **Budget alerts:** with `QUARTER_APPROVAL_WEBHOOK` set, the gateway checks the forecast at most once a minute after calls and posts once a day per cap when a named monthly cap has been or will be reached. The once-a-day memory resets on restart.

## Why a task cost that

`uv run quarter-ledger why <task>` prints where one task's money went. The task is `x-quarter-task`, or Claude Code's session id. It shows:
- the total charged, and the total at list price;
- what cache misses cost that a stable prompt prefix would have saved, at the model's cache prices;
- the shares of tool definitions (per MCP server), system prompt, history and output;
- cost per Claude Code subagent and per user prompt;
- each cache break: its turn, its cause and what it cost.

The same report is `GET /tasks/<task>/why` (JSON, amounts in micro-dollars, viewer role or above) and the MCP tool `why` (in USD). The dashboard lists the costliest tasks this month, each with a Why link.

How it works:
- The gateway keeps a record of every priced call: task, agent, model, tokens (input, output, cache reads and writes), cost, and billing route.
- Calls on a claude.ai subscription are charged $0. Their list price is kept beside it, so `why` still shows what they were worth.
- Each record holds a fingerprint of the prompt: a short hash and byte size of the system prompt, of the tool definitions per MCP server (from the `mcp__<server>__` prefix, else `builtin`), and of the message history, plus which blocks carry `cache_control`. Prompt and response text is never stored.
- With Claude Code's hint headers (`wrap` turns them on), each record also holds the subagent, its parent, the prompt id and the request class.
- A cache break is a call that reads less than half of what the previous call cached. The previous call is the last one in the same task, subagent and request class, since each has its own prompt. Calls without a task are recorded but not compared.
- The cause named is the first that fits, in this order: no cache marker (Anthropic only; OpenAI caches without one), model switched, tools added, removed, reordered or changed (naming the server), system prompt changed, earlier history changed, idle past the cache lifetime (5 minutes, or 1 hour for extended writes), and last "prefix unchanged: the vendor missed the cache".
- The shares are estimates. Prompt tokens are spread over the parts by byte size, in prompt order, and priced as the vendor read them. They always add up to the total.

**Cache collapse alert:** when a task's cache hit rate stays under half its own running average for 3 calls in a row, the gateway posts once to Slack (if set up) or else the approval webhook. Calls under 1,024 prompt tokens do not count. The memory of which tasks were alerted resets on restart.

## Runaway agents and cheaper models

- **Runaway-loop stop (on by default):** after each call, the gateway compares the agent's spend in the last 5 minutes with its usual spend per 5 minutes over the previous day. Over 10x usual and over $5, the agent is paused: its calls get `423 agent_paused` until someone resumes it from the dashboard, `uv run quarter-ledger resume <agent>`, or `POST /agents/<agent>/resume`. The webhook hears about each pause. `uv run quarter-ledger paused` lists them. Tune with `QUARTER_RUNAWAY='{"window_minutes": 5, "multiple": 10, "floor_usd": 5, "repeats": 20, "tool_repeats": 8}'`, or set it to `off`. A brand-new agent has no usual spend, so only the floor protects it.
- **Tool-call loop stop (on by default):** an agent loop grows its request every turn, so the identical-body check misses it. The gateway also reads the tool calls of the model's last turn in each request (Anthropic `tool_use`, OpenAI `tool_calls` and Responses `function_call`) and hashes each name with its arguments, and the result the request carries back. When one task makes the same tool call and gets the same result 8 times inside the window, that task is paused, not the agent. The same call with a changing result is usually work, such as tests rerun after each edit, so it pauses only at 24. Its calls get `423 task_paused` until someone resumes it from the dashboard, `uv run quarter-ledger resume <task> --task`, or `POST /tasks/<task>/resume`. Each tool call id counts once, so retries do not add up. Set the count with `"tool_repeats"` in `QUARTER_RUNAWAY`; the changing-result limit is three times it. A call with no task pauses its agent instead.
- **Cheaper model near a cap:** with `QUARTER_FALLBACKS='{"openai": {"gpt-5": "gpt-5-mini"}}'`, a call that a cap would refuse or send for approval is moved to the cheaper model if that fits, in the same step. The reply carries `x-quarter-model: gpt-5 -> gpt-5-mini`. An agent can send `x-quarter-fallback: off` to be refused instead. Both models need a price.
- **Prompt caching, added for you (on by default):** Anthropic bills a cached prompt prefix at a tenth of the input price, but only when the request asks. An Anthropic conversation (two or more messages) that sends no cache marker gets the top-level one, `"cache_control": {"type": "ephemeral"}`. The cache write costs 1.25 times the input price, so it pays from the next turn; one-off calls are left alone. Clients that already mark their prompts, such as Claude Code, are not touched. Each call records what the marker saved at list price (`saved`, below zero on a last turn whose write nobody read), and `status` shows the month's total. The reply carries `x-quarter-cache: added`. Send `x-quarter-cache: off` to skip one call, or set `QUARTER_AUTO_CACHE=off`. Anthropic's own API only: the old Bedrock integration refuses the field.

## The Quarter feed: prices and rules kept current

Vendors change prices, and agents find new ways to do damage, faster than a release ships. Set `QUARTER_FEED` (an https address) and `QUARTER_FEED_KEY` (the `ed25519-pub:` key it is signed with), and the gateway fetches the feed when it starts and once a day.

- **What it holds:** prices, and tool rules that hold a destructive tool call for a person. A feed rule can only ask (`approve`), never deny, and holds no call rules, routes or redactions, so Quarter can make a call wait but never stop one you allowed.
- **API calls too (feed version 2):** HTTP rules hold a destructive call to an API, such as Railway's `volumeDelete` or a GitHub repository delete, for a person. They match where the call really goes: host, method, path, and where needed the query, the body or the GraphQL fields. They apply only to calls none of your call rules matched, so your `allow` stands, and an upstream marked `"default": "deny"` still denies. `status` lists them as `feed rule <id>`. The feed repository's `COVERAGE.md` lists what is held.
- **Payees too (feed version 4):** the feed lists x402 payees with the prices they list. `POST /authorize` sends a payment to a person when the payee is marked `caution`, or when it is more than ten times the listed price for the resource paid for, with the feed's reason, which the approval records. Send `network` and `resource` with the payment for the price check; the x402 hooks do. A payee the feed does not list is not held.
- **Yours wins:** feed prices fill in models your price file does not set, over the built-in ones. Feed rules come after your own, so your rule for the same tool decides first. `status` lists them as `feed rule N`.
- **Safe to depend on:** a feed that does not verify, is malformed, or is no newer than the one held is ignored and audited (`feed_refused`). The last good feed is kept beside the ledger (`<db>.feed.json`), so a gateway that restarts offline keeps it.
- **On the record:** each feed taken is an audit row (`feed_applied`) with its version and hash, in the hash chain, so you can show which rules were in force on any day.
- Quarter signs a feed with `quarter-ledger feed-sign <body.json>` and `QUARTER_FEED_SIGNING_KEY`. The feed is a static file: hosting it costs nothing.

## Evidence: tamper-evident ledger and audit pack

- **Tamper-evident ledger:** every ledger entry and audit-log row carries a SHA-256 hash of itself and the row before it. An edited, inserted or deleted row breaks every hash after it. `uv run quarter-ledger verify-chain` recomputes both chains and exits 1 if either is broken. Someone with write access could rewrite the whole chain, so record the head hash outside Quarter (`anchor` below writes it, and the audit pack prints it); a later chain that does not contain it means history was rewritten. Databases from before this change start their chain at the first new row.
- **Anchor:** `uv run quarter-ledger anchor --out anchor-2026-11-03.json`. It records each chain's head row, the number of chained rows up to it and its hash, with one SHA-256 digest of them, in the audit log and in that file. Keep the file outside Quarter. Anyone holding it checks a later ledger, backup, audit pack or incident pack offline with `quarter-ledger anchor verify <file> --anchor anchor-2026-11-03.json`. It recomputes the chains and exits 1 unless each anchored head is there, at its row. A month's audit pack leaves rows out, so it proves less than a backup; `linked` says whether the head row itself was recomputed. With `--submit`, or with `QUARTER_ANCHOR_CALENDAR` set to a calendar URL, the digest alone goes to an OpenTimestamps calendar (default `https://a.pool.opentimestamps.org`), and a pending proof is saved beside the file as `.ots`. No ledger data is sent. It is off by default and gives up after 5 seconds with no answer. A failure is recorded as `not submitted`, and the anchor is still made. A few hours later, `ots upgrade anchor-2026-11-03.ots` completes the proof and `ots verify -d <digest> anchor-2026-11-03.ots` checks it against Bitcoin, with the standard OpenTimestamps client. Run it daily: see `docs/DEPLOY.md`.
- **Audit pack:** `uv run quarter-ledger audit-pack --month 2026-10 --out pack.zip`. A zip with every entry and its hash, approvals and who decided them (signed receipts if `QUARTER_SIGNING_KEY` is set), the audit log, pauses, the caps in force, the journal, the month of the approval record (`approval_record/`, see below) and a fresh chain check. Built for SOX walkthroughs and SOC 2 evidence requests. `controls.md` (and `controls.json`) maps ten AIUC-1 controls (the AI agent standard insurers certify against: A006, B006, B007, C007, C009, D003, E004, E009, E011, E015), SOC 2 CC6, CC7 and CC8 and EU AI Act Articles 12, 19 and 26 to that evidence with counts, and warns when retention is unset or under six months or a chain is broken. Quarter provides evidence; it does not certify compliance.
- **Approval record:** one record of approvals from every service. Stripe: set `QUARTER_STRIPE_APPROVALS_SECRET` to the `whsec_` secret of a thin event destination that sends `v2.core.approval_request.*` to `POST /approvals/stripe`. Each signed event is added to the audit chain once; a bad or stale signature gets 401. Stripe's event says which request and what happened, not who asked, who decided or how much. Other sources: `uv run quarter-ledger approvals import auth0.jsonl`, one JSON object a line with `source`, `id`, `decision` (requested, approved, denied, canceled, expired, succeeded or failed) and `at`, plus `requester`, `approver`, `action`, `ref`, `amount_usd` and `reason` if known. `uv run quarter-ledger approvals export --out approvals.zip` writes every approval, Quarter's own included, with its chain hash and a chain check; `quarter-ledger approvals verify approvals.zip` recomputes each row's hash from the file alone. The monthly audit pack holds the same files under `approval_record/`, listed under SOC 2 CC8.1, and `approvals verify pack.zip` checks them there. Quarter only records these approvals. It never approves anything in Stripe or any other service.
- **Underwriting statement:** `uv run quarter-ledger underwriting --month 2026-10 --out statement.json`. One month of agent risk as counts an insurer can rate on, in the schema `quarter.underwriting/1`. It covers exposure (calls, agents, tasks, tokens and cost by vendor, model and route), authority (the caps and rules in force, and agents with a named owner), high-impact actions (upstream calls by method and decision, writes, tool calls held), oversight (approvals and how they were decided) and containment (loop and runaway stops, budget alerts). It also holds both chain checks and head hashes. It contains no prompts, commands, request paths, people or customer names; the rule lines it lists are your own, so they can name agents and path patterns. With an `ed25519:` signing key it is signed; give the insurer the public key (`ed25519-pub:...`) and they check it with `QUARTER_SIGNING_KEY=<public key> quarter-ledger verify <signed>`. Its fields follow the telemetry insurers ask for in *Underwriting the Agent Economy* (July 2026). No insurer rates on it yet.
- **Incident pack, for a claim:** `uv run quarter-ledger incident --from 2026-10-14 --to 2026-10-15 --agent deployer --out incident.json`. The record around a loss: the agent's or task's calls, ledger entries and audit rows in the window (upstream calls, held tool calls, pauses). Each chain is linear, so a row is proven only by the rows after it, and the pack carries both chains from the window to now. The insurer checks it with `quarter-ledger check-incident incident.json --statement statement.json`, one `--statement` for each month they hold. It reports, per chain, whether it recomputes and how many rows in scope come before a head hash from a statement they already had. Those rows were in the ledger, as written, when that statement was made; a history rewritten later matches no head they hold. Rows after the window travel too, since the proof needs them, including other agents' rows and customer names: whoever sends the pack decides who sees them.
- **Call ids:** every allowed call returns its id in `x-quarter-call`, the `call_id` of its rows in the ledger and in the audit pack.

## One policy for every way agents pay

API calls go through the gateway. Payments by other means (x402, MPP, cards, invoices) get the same caps, approvals and pauses through two calls. Quarter never holds keys or money; the customer's own wallet or card pays.

1. **Before paying**, the agent calls `POST /authorize` with its usual `x-quarter-*` headers and `{"rail": "x402", "payee": "0x...", "amount_usd": "0.25"}` (or `amount_micros`), optionally with the x402 `network` and the `resource` URL paid for. Rails: `x402`, `mpp`, `card`, `invoice`, `other`. Quarter holds the amount and answers with `call_id` (also in the `x-quarter-call` header) and a signed `authorisation` valid for 10 minutes, or 402, 403 (approval) or 423 like any call. The same token is also under `authorization`; the British `authorisation` is kept so existing clients keep working. With `x-quarter-wait: <seconds>` (at most 300) a payment that needs approval waits for the person's decision instead of answering 403 at once. It works with nothing to set up: the signing key is made on first run and its public key printed, for the signer below.
2. **The customer's own signer checks it** before signing the payment:

   ```python
   from quarter_ledger.payments import authorised

   if not authorised(token, SIGNING_KEY, pay_to, amount_micros):
       raise PermissionError("Quarter did not authorise this payment")
   ```

3. **After paying**, the agent calls `POST /settle` with `{"call_id", "amount_usd", "tx"}` (or `amount_micros` in place of `amount_usd`). The hold becomes the real charge. Paying more than authorised is recorded and flagged in the audit log. Settling `0` releases the hold. Until settled, the hold counts against caps.

- **x402 client hooks:** `x402/` is the npm package `@quarterfi/x402-spend-control`. One call, `withQuarter(client, { url, key, agent })`, puts both steps inside the official x402 client: each payment is authorised before it is signed, carries the mandate below, and is settled after, at 0 when it failed. Its `cloudflare-agents` export is the confirmation callback for `withX402Client` in the Cloudflare Agents SDK.
- **x402 helpers:** `x402_options(body)` reads a v1 402 body, `x402_options(header=...)` a v2 `PAYMENT-REQUIRED` header, giving each option's network, asset, payee and amount in atomic units. `stablecoin_micros(amount, decimals=6)` turns a USD stablecoin amount into micro-dollars (USDC's atomic unit is one micro-dollar).
- **Payee lists:** `QUARTER_PAYEES='{"x402": ["0xabc..."]}'`. On a listed rail, a payment to anyone else waits for a person, with the reason "not on the payee list"; approved, it pays that payee once. Add `"unlisted": "deny"` to refuse those instead (`403 payee_not_allowed`). An approval covers only the payee, or vendor, it was given for.
- Payments appear in every report and forecast as vendor `rail:payee`.

### Agent wallets: checked on chain, and refusing on their own

Agents that pay by x402 pay from the company's own wallets. Two commands check those wallets and harden them. Neither needs an account or a key, and Quarter still holds no money.

- **Reconcile:** `uv run quarter-ledger wallets reconcile --month 2026-10 --wallets wallets.json`, where `wallets.json` is `{"0x... wallet address": "agent name"}`. It reads the month's USDC Transfer events out of those wallets on Base and matches each to an x402 settlement: by the `tx` the agent reported to `/settle`, otherwise by the same agent paying the same payee within an hour. It lists transfers Quarter never authorised (spend outside the gateway), settlements with no transfer, and transfers whose payee or amount differs from what was settled, and exits 1 if there is any. USDC has 6 decimals, so on-chain amounts are micro-dollars as they are.
  - It uses Base's public endpoint `https://mainnet.base.org` unless `--rpc` or `QUARTER_BASE_RPC` names another. On 2026-11-03 that endpoint answered block lookups but refused `eth_getLogs` with HTTP 429 every time, so in practice use a provider's endpoint; put a URL that holds a key in `QUARTER_BASE_RPC`, not on the command line. Logs are read 2,000 blocks at a time (Base's advice), halving when an endpoint says the range is too large, and a 429 is retried four times.
  - Only Base mainnet USDC, only agents listed in the file. Settlements reported on another network are left out.
- **Wallet policy:** `uv run quarter-ledger wallets policy --format cdp [--agent researcher]` prints a Coinbase Developer Platform Policy Engine document (the body of CDP's Create policy request) made from the caps and the `QUARTER_PAYEES` `x402` list. Create it in CDP yourself and attach it to the agents' server wallets; the wallet then refuses what Quarter would refuse even if an agent skips the gateway. It accepts only USDC on Base: an x402 signature (EIP-3009 `TransferWithAuthorization`) or a USDC `transfer`, to a listed payee, of at most the smallest blocking cap that counts every payment (`all`, or the agent's own with `--agent`, which also makes it an `account` policy). Every other transaction or typed-data signature, and every raw hash signature, is refused. Caps that ask a person, and task or customer caps, are left out, since they do not refuse for certain. A person can approve a payee off Quarter's list; the wallet cannot, so add such payees to the list first.
  - **Untested against a live CDP account.** None exists yet. The shape follows CDP's published schema (https://docs.cdp.coinbase.com/api-reference/v2/rest-api/policy-engine/create-policy); CDP's own simulate endpoint is the next check.
- **Privy wallet policy:** `--format privy` prints the same rules as a Privy policy (the body of Privy's Create policy request, `POST https://api.privy.io/v1/policies`). Create it in Privy yourself and list it in each agent wallet's `policy_ids`. It allows `eth_signTypedData_v4` for a `TransferWithAuthorization` on Base's USDC domain, and `eth_sendTransaction` or `eth_signTransaction` of a USDC `transfer` on chain 8453 with no ETH, to a listed payee, of at most the same cap. Privy denies any method a policy has no rule for, so every other signature, transaction, user operation and key export is refused without a rule of its own. With `--agent` only the name and the cap change, since Privy policies have no scope.
  - Privy matches a typed-data condition only when the request's `types` map is exactly the policy's. There are two rules: one for the Privy x402 clients, which send only `TransferWithAuthorization`, and one for a viem `WalletClient`, which also sends `EIP712Domain` with `name`, `version`, `chainId` and `verifyingContract`. Any other shape is refused.
  - Left out: a payee list longer than 100 (Privy's `in` takes 100; longer lists need a condition set, which this does not create), Privy's high-level `transfer` action, and an `owner_id`. Add an owner in Privy so the app secret alone cannot change the policy.
  - **Untested against a live Privy account.** None exists yet. The shape follows Privy's published docs, read 2026-11-03: [Create policy](https://docs.privy.io/api-reference/policies/create), [policies overview](https://docs.privy.io/controls/policies/overview), [Ethereum examples](https://docs.privy.io/controls/policies/example-policies/ethereum), [x402 sanctions screening](https://docs.privy.io/recipes/agent-integrations/x402-sanctions-screening) and [template variables](https://docs.privy.io/controls/policies/template-variables), which says address fields compare EVM addresses without regard to case.

### Agent cards on Mercury and Ramp

Quarter does not decide swipes on Mercury or Ramp agent cards. The agent asks `POST /authorize` with rail `card` before it pays, then pays with its card. Reconcile reads the card feed with your own read-only credentials and ties each charge to the authorisation behind it.

- **Run it:** `uv run quarter-ledger cards reconcile --month 2026-10 --source mercury --cards agent-cards.json` (or `--source ramp`). `agent-cards.json` is `{"card id": "agent name"}`. On Ramp a fund id or the agent's Ramp user id also names an agent, since Ramp may issue a new card for each purchase.
- **Credentials:** Mercury takes a Read Only token in `QUARTER_MERCURY_READ_TOKEN` (or `QUARTER_MERCURY_TOKEN`); a Read Only token needs no IP allow list. Ramp takes a Developer API app with the Client Credentials grant and the `transactions:read` scope, in `QUARTER_RAMP_CLIENT_ID` and `QUARTER_RAMP_CLIENT_SECRET`; `QUARTER_RAMP_URL=https://demo-api.ramp.com` points it at the sandbox. Credentials are never arguments and never printed.
- **Matching:** a charge matches a card authorisation from the same agent made between 72 hours before the charge and 5 minutes after it. The charge may be up to 5% or $1 over the authorised amount, whichever is more, to allow for currency conversion or a tip. The closest amount wins. One authorisation covers one charge, so a second charge against it is outside.
- **Output:** matched charges with the task, customer, approver and the payee the agent named; charges outside Quarter; charges more than the tolerance over what was authorised; refunds and credits; and authorisations not charged that month. It exits 1 when any charge is outside or over.
- **Refunds:** listed, never matched or flagged. Charges that never moved money (Mercury reversed, cancelled, failed or blocked; Ramp declined or errored) are left out.
- **`--settle`:** records each matched charge whose authorisation is still open as its settlement at the charged amount, with a `payment_settled` row in the audit chain, so it reaches the month-end journal. Pending charges and charges over the tolerance are left for a person. Without it, reconcile only reads.
- Sources: Mercury [listTransactions](https://docs.mercury.com/reference/listtransactions) and [tokens](https://docs.mercury.com/reference/getting-started-with-your-api); Ramp [transactions](https://docs.ramp.com/developer-api/v1/api/transactions), its [OpenAPI document](https://docs.ramp.com/openapi/developer-api.json) and [authorization](https://docs.ramp.com/developer-api/v1/authorization). Tested against those documented shapes, not yet against a live account.

### Spend mandates: proof for the seller

The authorisation above is for your own signer. A spend mandate is what your agent shows the seller: signed proof that your company's spend policy allowed this payment. It names your company by its domain and leaves out the agent, the customer and the approver's name.

1. Set `QUARTER_ORG` to your company's domain, such as `example.com`. It needs an `ed25519:` signing key, which is the default.
2. Publish `uv run quarter-ledger mandate-keys` at `https://<your domain>/.well-known/spend-mandate-keys.json`. That file holds only the public key.
3. `POST /authorize` then also returns `mandate`, valid for 10 minutes. The agent sends it inside the x402 payment, as the `company-policy` extension: `PaymentPayload.extensions["company-policy"].info.mandate`. A seller asks for it by listing that extension in its 402 answer; the client echoes what the seller sent and adds the mandate. The `Spend-Mandate` header is still sent and read for one release. It is deprecated.

The mandate says: the company (`iss`), the payee (`aud`), the rail, the most it may charge (`max_amount_micros`, USD), which caps and rules were in force (`policy`, the hash of the recorded config), where the spend sits in the ledger (`entry`, the hold's chain hash), and the approval id if a person approved it. Your auditor can match `policy` and `entry` to the ledger.

A seller checks it with `sellers/verify-spend-mandate.mjs`, one file with no dependencies (Node 18 or later). The same folder is the npm package `@quarterfi/spend-mandate` (Apache-2.0), published from `sellers/` with `npm publish`:

```js
import { mandateFrom, verifySpendMandate } from './verify-spend-mandate.mjs';
const token = mandateFrom(req.headers['payment-signature']) ?? req.headers['spend-mandate'];
const claims = await verifySpendMandate(token, { payee: PAY_TO, amountMicros });
if (!claims) return refuse();  // claims.iss is the company; accept one payment per claims.sub
```

It fetches the keys from the company's domain and caches them for 5 minutes. It returns null for a forged, expired, wrong-payee or too-small mandate, and throws when the keys cannot be fetched. In Python: `quarter_ledger.mandate.verified(token, keys, payee, amount_micros)`, with the token from `mandate.from_payment(payment_signature_header)`. `sellers/test-vectors.json` holds signed mandates and what a seller must decide for each, so another implementation can check itself. From the terminal: `uv run quarter-ledger verify-mandate <token> [--keys keys.json] [--payee 0x...] [--amount-usd 0.25]`.

For a seller whose API runs on Fetch-style handlers (Cloudflare Workers, Hono, Next.js route handlers, Deno, Bun), `spendMandateGuard` does all of it in front of the handler in one call:

```js
import { spendMandateGuard } from './verify-spend-mandate.mjs';
const guard = spendMandateGuard({ payee: PAY_TO, trust: ['example.com'] });

export default {
  async fetch(request) {
    const { claims, refused } = await guard(request, priceMicros);
    if (refused) return refused;  // 402 no mandate, 403 invalid or untrusted, 409 used, 503 keys unreachable
    return serve(request, claims);  // claims.iss is the paying company
  },
};
```

`trust` lists the company domains you accept; leave it out to accept any company that signs. `required: false` lets calls without a mandate through. Each mandate pays once: the guard remembers it until it expires, in memory, so with several processes pass `seen: async (id, exp) => ...` backed by a shared store, returning true the first time an id is seen. When the keys cannot be fetched it answers 503 rather than letting the call through.

This is Quarter's own format, version 0. It follows the idea of the AP2 and Verifiable Intent mandates but adds what they lack: which company's policy allowed the spend. No seller checks it yet.

### Bill pay from your own account

Bills get the same caps, payee list and approvals as any agent spend, plus three checks accounts payable relies on. The money goes from your own bank account; Quarter never holds it.

- **Enter a bill:** an agent posts `POST /bills` with its agent key and `{"payee", "invoice", "amount_usd", "account", "due", "memo"}`, or a person runs `uv run quarter-ledger bill add --payee Acme --invoice INV-1 --amount-usd 120.50 --account <account> --due 2026-11-20`. `account` is where the money goes: a Mercury recipient id, or the bank details printed on the invoice.
- **The checks:**
  - An invoice number already entered for that payee is refused (409 `duplicate_invoice`).
  - The same amount to the same payee within 30 days waits for a person, as a possible duplicate.
  - A first payment to a payee's account, or a bank account that differs from the last one paid, waits for a person, and a change is written to the audit log. Changed bank details are how invoice fraud works: confirm them with a contact you already know.
- **Status:** approved (held against the caps), waiting (an approval is open, in Slack or the dashboard), or refused. `bill list` and `GET /bills` (viewer) show them.
- **Pay:** `uv run quarter-ledger bill pay <id> --by <you>`, or `--due-by 2026-11-30` for every bill due by then, or `POST /bills/<id>/pay` (finance role, with a person's own key: the shared admin key names no one, so it cannot pay). The person paying cannot be the one who approved the bill, so approving and releasing a payment always takes two people. No single bill, payment or call may be over $1 billion.
  - `--method mercury` (the default) sends it from your Mercury account: set `QUARTER_MERCURY_TOKEN` (a token with the Send Money scope; Mercury asks you to allow the server's IP) and `QUARTER_MERCURY_ACCOUNT`. `--payment-method ach`, `check`, `domesticWire` or `realTimePayment`. The bill id is Mercury's idempotency key, so a retry never pays twice.
  - `--method manual --ref <bank reference>` records a payment you made from any bank.
- Paid, the hold becomes the charge, and the bill is in every report and the journal as vendor `invoice:<payee>`.
- **What is kept:** bank details only as a fingerprint and the last four characters. A Mercury recipient id is kept as it is, since paying needs it.
- **W-9s:** `uv run quarter-ledger bill payee-tax Acme --form-date 2026-01-10 --entity individual [--pays-for services]` records the W-9 on the payee's record and asks for the TIN (or reads it from standard input), so it never reaches shell history. Entity is `individual` (or sole proprietor), `partnership`, `llc-partnership`, `c-corp`, `s-corp`, `other` or `foreign` (a W-8, no TIN asked). `--pays-for` is `services`, `goods`, `attorney` or `medical`. The TIN is kept only as a fingerprint keyed by the signing key, and its last four digits; the full number is never stored, logged or printed.
- **No W-9:** when a bill to a payee with no W-9 is approved or paid, the audit log gets a `w9_missing` warning (backup withholding may apply) and `bill list` marks it `(no W-9)`. It never blocks the payment: whether to pay without one is your policy.
- **1099s:** `uv run quarter-ledger tax-1099 --year 2026 [--csv 1099.csv]` lists every payee paid that calendar year (paid bills only, by payment date in UTC), the total, whether the W-9 is missing, and whether a 1099-NEC or 1099-MISC is due: `yes`, `no`, or `check` when Quarter cannot tell (no W-9, entity `other`, a foreign payee, or a corporation not marked as attorney or medical). The threshold is $600 for payments made in 2025 and earlier, and $2,000 after 31 December 2025 ([Publication 1099](https://www.irs.gov/publications/p1099), [Rev. Proc. 2025-32](https://www.irs.gov/pub/irs-drop/rp-25-32.pdf)). It is indexed for inflation from 2027; that figure is not published yet, so $2,000 is used, which can only report more. Corporations are exempt except attorneys' fees and medical payments, and gross proceeds of $600 or more to an attorney are flagged ([Instructions for Forms 1099-MISC and 1099-NEC](https://www.irs.gov/instructions/i1099mec)). Card payments are reported by the card network on Form 1099-K: bills are paid by bank, so none is a card payment, and Quarter card spend is not in the report. Record only bank payments as manual payments. This is a worklist for your accountant, not a filing.

### Quarter cards on Stripe Issuing

A company that issues cards to its agents through Stripe Issuing (the self-serve program for its own business) can have Quarter decide every swipe. Quarter never holds the money: the company funds its own Issuing balance and pays Stripe's $0.10 a virtual card.

1. Set `QUARTER_STRIPE_SECRET` to the signing secret (`whsec_...`) of a Stripe endpoint that sends Issuing events to `POST /issuing/stripe`, and make that endpoint the Issuing authorization webhook. Without the secret the route answers 503; an event with a bad signature, or older than 5 minutes, gets 401.
2. Name the agent on each card in its metadata: `quarter_agent`, and optionally `quarter_task` and `quarter_customer`. A card without them counts as agent `card-<last 4>`.
3. Each `issuing_authorization.request` is held against the caps like any call, as vendor `card:<merchant>`, and answered at once with `approved`. Stripe allows 2 seconds, so a person cannot approve inside it: a swipe over the approval line is declined and opens an approval, and once a person approves, the next swipe for that agent and merchant, up to the approved amount, goes through once. Merchants not on `QUARTER_PAYEES` `card` list are treated the same way. Only US dollar amounts are checked; anything else is declined.
4. The answer's metadata carries `quarter_call` (the hold), `quarter_approval` or `quarter_reason`. When the authorization closes (`issuing_authorization.updated` with status `closed`, `reversed` or `expired`), Quarter charges what was captured and releases the rest. Repeated events are ignored.

Set Stripe's timeout setting to decline, so a swipe is never approved while Quarter is down.

### Sanctions screening (OFAC)

Every payee is screened against the US Treasury's OFAC lists before Quarter allows the payment: `POST /authorize`, card swipes and bills.

- **Get the lists:** `uv run quarter-ledger sanctions update` downloads the SDN list and the Consolidated (non-SDN) list from OFAC's Sanctions List Service (`SDN.XML` and `CONSOLIDATED.XML` at `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/`). It keeps them in `<db>.sanctions/` (or `QUARTER_SANCTIONS`), with each list's publish date and SHA-256, and writes that to the audit log. A download that does not parse, or holds fewer records than it says, changes nothing. Run it daily, from cron. The gateway never fetches; it reads the stored copy and picks up a new one without a restart.
- **Matching:** names are compared without case, accents, punctuation, business suffixes (LLC, Ltd, SA, and so on) or word order. Primary names and strong aliases count. Weak aliases are left out, as OFAC allows (FAQ 124). Vessels and aircraft are left out. A close match is a token-sorted similarity of 0.9 or more. Wallet addresses listed in the SDN data are matched exactly, hex addresses in any case.
- **Decisions:**
  - Refused: a wallet address on the SDN list, or the exact name of an SDN entity (`403 sanctions_match`, or a declined swipe).
  - Waits for a person, who sees the entry, its id, its programs and the score: an exact match on a person's name, a close match, or any match on the Consolidated list. A name alone does not identify a person: "Hassan Ali" is on the SDN list and is a common name. The Consolidated list's programs mostly restrict rather than block.
  - An approval covers one payment, like any approval. The next payment to the same payee is screened again.
  - Cleared payees: when a named person approves a payment held only for a close match, Quarter remembers that payee as cleared for that entry and stops holding it. The audit log records who cleared it (`sanctions_cleared`). Exact matches are never remembered, and neither is an approval made with the shared admin key. When `sanctions update` finds the entry changed or gone from the list, the payee is forgotten, so a new listing is held again.
  - `uv run quarter-ledger sanctions cleared` lists cleared payees and who cleared them. `sanctions forget "<name>"` makes the next payment wait for a person again.
  - Shadow mode does not pass any of these.
- **Stale lists:** older than 7 days since the last download (`QUARTER_SANCTIONS_STALE_DAYS`), a payment to a new payee waits for a person. A payee paid before, or on the payee list, goes on. Nothing passes unscreened: every result is in the audit log with `stale` set. `status` and the gateway's start show the list dates and say STALE.
- **No lists stored:** payees are not screened, and `status` and the gateway's start say so.
- **Audit:** every screen writes a `sanctions_screen` row to the hash-chained audit log: payee, rail, outcome, the entry and name matched, the score, and each list's publish date and hash.
- **Test a name:** `uv run quarter-ledger sanctions check "Banco Nacional de Cuba"` prints the match and what a payment would get.

This is a screening aid, not a certified sanctions compliance program. It does not cover:

- Other lists: EU, UK (OFSI), UN, or any other country's.
- OFAC's 50 percent rule: companies owned by listed persons are not named on the list.
- Rescreening payees already paid when a list changes. Each payment is screened when it is made, so a payee added to the list is caught on the next payment, not before.
- Partial names and spelling variants that reorder the words, such as a missing middle name or Hezbollah for Hizballah.
- Checking a match against date of birth, address or country. A person does that.
- Card merchants named only by a descriptor or a network id.

## Change log

Name the person responsible for each agent with `quarter-ledger owner <agent> <person>`. Approvers are named with `approve <id> --as <person>` or the `x-quarter-approver` header; an owner cannot approve their own agent's spending (they can deny it), and refusals are audited.

Every time the gateway starts, it hashes each settings section (caps, prices, upstreams, runaway, payees) and writes a `config` entry to the hash-chained audit log when any differs from the last start, naming which changed. This is the change-management evidence SOX and SOC 2 auditors ask for.

## Agent register and quarterly review

Every agent should have a named owner, and someone should confirm each quarter that it is still needed. Quarter keeps that list and the record of each review.

- **The register:** `uv run --env-file .env quarter-ledger agents list` shows every agent Quarter knows: those with a named key in `QUARTER_AGENT_KEYS` and any agent seen in the ledger. For each it gives the owner, the caps and payee rules that apply, when it was first seen, its last call and last payment, this month's spend, its last review and a status. `--csv` writes all of it as a file. The statuses are `ok`, `no owner`, `dormant` (no call or payment in 30 days), `review due` (last quarter's review is still missing, within the grace days), `review overdue` (still missing after them), `retired`, `never used` and `seen without a key`. The dashboard shows the same register in short.
- **The review:** `uv run --env-file .env quarter-ledger agents review --quarter 2026-Q4 --agent researcher --by Sam --keep` confirms the agent is still needed and rightly owned. `--retire` instead pauses it, so its key gets `423 agent_paused` everywhere until someone resumes it; remove the key from `QUARTER_AGENT_KEYS` too. `--note` adds a reason. Each review is a row in the hash-chained audit log (`agent_review`).
- **Who may review:** the agent's owner, or anyone who may approve (a person above viewer in `QUARTER_PEOPLE`, a Slack decider, or `admin`). An owner may confirm their own agent, since that is what an owner review is. The agent itself never may, and keeping an agent needs an owner. A refused review is audited as `review_refused`. Like `approve --as`, the name comes from whoever runs the command.
- **When it is due:** each quarter's review is due 14 days into the next quarter. An agent that only started this quarter is not due until next quarter.
- **Sending agents to a person:** set `QUARTER_REGISTER=on`. Then the next call or payment of an agent with no owner, unused for 30 days, or past its review waits for a person, with the reason "agent has no owner", "agent unused for 30 days" or "quarterly review overdue". It is never refused. Approving it lets that one call through and is not a review, so the next call asks again until the owner is named or the review is done. `QUARTER_REGISTER=shadow`, or `QUARTER_MODE=shadow`, only records a `would_hold` row. `{"dormant_days": 30, "grace_days": 14, "mode": "enforce"}` changes the limits. It is off unless set, so that agents with no owner keep working on the day Quarter is upgraded. It adds one indexed lookup to each call, about 7 microseconds.
- **For the auditor:** the audit pack's `controls.md` and `controls.json` end with the reviews for that month's quarter and the one before: each agent kept or retired, its owner, who reviewed it and when, the agents active and not reviewed, and those past their review or with no owner when the pack was made. They are mapped to AIUC-1 E004, SOC 2 CC6.2 and CC6.3. Model calls and payments are routed; calls to APIs that are not models are not yet.

## Teams and roles

Give each person their own key and a role with `QUARTER_PEOPLE` (JSON text or a path):

```json
{"Sam": {"key": "...", "role": "approver"}, "Priya": {"key": "...", "role": "finance"}}
```

- **viewer:** the dashboard and every report (`/approvals`, `/audit`, `/forecast`, receipts).
- **approver:** also approves and denies, and resumes paused agents. The owner rule above still applies.
- **finance:** also records costs, downloads the close files and edits the cost centres on the dashboard.
- **admin:** everything.

A person signs in to the dashboard with their key as the password (any username), or sends it as `x-quarter-key`. The dashboard says who is signed in and leaves out buttons their role cannot use; the server refuses those actions anyway with 403, audited as `role_refused`. Keys are compared in constant time and must differ from each other and from agent keys.

Every action through the dashboard or API is audited with the person's name and address, such as `Sam@10.0.0.4`, and an approval's `decided_by` is the person who decided it. `x-quarter-approver` is not needed with a person's key; one naming someone else is refused (403, audited as `approver_mismatch`). Names and roles, never keys, go into the `config` audit entry, so changes to the team show up in the change log.

`QUARTER_ADMIN_KEY` keeps working: it signs in as an admin named `admin`, and may still name the approver with `x-quarter-approver`. No one in `QUARTER_PEOPLE` may then be called `admin`.

**Retention:** `QUARTER_RETENTION_DAYS=2555` states the minimum number of days the ledger promises to keep every entry. Quarter has no command or endpoint that deletes entries, so nothing can break that promise from inside Quarter. The setting is written to the `config` audit entry and to the audit pack's `chain.json` (`retention_days`), so auditors can see it.

## Backup and restore

The whole ledger is one SQLite file, `ledger.db`. Do not copy it with `cp` while the gateway runs: recent writes live in `ledger.db-wal`, and a plain copy can miss them or catch them half written.

- **Backup:** `uv run quarter-ledger backup backups/ledger-2026-11-01.db`. It uses SQLite's online backup, so the gateway keeps running and the copy is one consistent snapshot. The copy is readable by its owner only, an existing file is never overwritten, and both hash chains are checked in the copy (exit 1 if either is broken). In Docker, see `docs/DEPLOY.md` for a nightly cron line.
- **Keep copies elsewhere:** on another machine or bucket, with the head hash from `audit-pack` or a file from `anchor`. Run it daily, for example from cron.
- **Restore:** stop the gateway. Check the copy with `uv run quarter-ledger --db <copy> verify-chain`. Move `ledger.db`, `ledger.db-wal` and `ledger.db-shm` aside (keep them), copy the backup to `ledger.db`, `chmod 600 ledger.db`, and start the gateway. Anything recorded after the backup is not in it.

## If the ledger is down

By default Quarter refuses every call it cannot record. While the ledger cannot be written (the disk is full, the file is locked or gone), agents get `503 ledger_unavailable`.

`QUARTER_FAIL_OPEN=model` keeps plain model calls working instead:

- **What passes:** a model call that nothing would send to a person. It goes to the vendor and carries `x-quarter-fail-open: model`.
- **What stays closed:** tool-call holds, call rules that ask a person, payments, bills, cards, a call over a cap's `approve_above_usd`, a retry with `x-quarter-approval`, and every call while `QUARTER_REGISTER` is on. Caps over a window cannot be checked without the ledger, so a passed call is not limited by them.
- **The fallback file:** each passed call is written to `QUARTER_FAIL_OPEN_LOG` (default `ledger.db.fail-open.jsonl`, owner-only) before it is sent, and again once it is settled. Each line is synced to disk first. If the file cannot be written, the call is refused.
- **Booking it later:** once the ledger is back, run `uv run quarter-ledger ingest-fail-open`. Each call is booked at the time it was made, with a `fail_open` row in the audit log. A call with no settled line is charged its full hold, so spend is never undercounted. Running it twice books nothing twice. The file is then renamed `.ingested-<time>`.
- **Seen by everyone:** `serve` prints a `FAIL-OPEN` line, and the setting is part of the config in the change log.

It covers a ledger that fails while the gateway runs. A ledger that cannot be opened stops `serve` from starting.

## Configuration

- **`caps.json`:** optional; with none, spend is recorded but not limited. The gateway reads it at start, so restart `serve` after an edit. A list. Each item has:
  - `scope`: `agent`, `task`, `customer`, or `all` (all spend together, as `init` writes).
  - `key`: an id, or `*` (the default) for each one.
  - `window`: `day`, `month` or `total`.
  - `limit_usd`.
  - `approve_above_usd` (optional).
  - `over` (optional): `block` (the default) refuses a call that would pass the cap; `approve` sends it to a person, who may let it through. `init` writes `approve`. A cap that blocks wins over one that asks, and the person asked sees every reason.
  - `mode` (optional): `enforce` (the default) or `shadow`. See Trying Quarter without breaking anything.
- **`prices.json`:** optional. List prices for OpenAI, Anthropic, Gemini and OpenRouter are built in (`src/quarter_ledger/prices.json`, with the date checked and the pages they came from). Your file adds vendors and overrides models, so a negotiated rate goes here.
  - Token prices: `input_per_million` and `output_per_million`.
  - Prices that rise with prompt length: `long_context` with `above_tokens` and the higher prices.
  - Flat prices: `per_request`.
  - `*` covers every model of that vendor.
  - Where a vendor lists two prices the gateway cannot tell apart (audio against text input, or a rise already announced), the built-in table uses the higher one.
  - A model with no price is never treated as free. The call goes through, held and charged at the highest input and output prices known for that vendor (any vendor, if it has none), and is audited once a day as `unknown_price`. Add its price to stop overpaying.
- **`upstreams.json`:** optional, read only when named by `--upstreams` or `QUARTER_UPSTREAMS` (`prices.json` likewise, by `--prices` or `QUARTER_PRICES`). Vendor name to base URL, added to the four built in. A value can also be `{"url": ..., "auth": "Project-Access-Token", "headers": ["X-GitHub-Api-Version"]}`: `auth` is the header an API takes its key in (the agent sends its Quarter key there and Quarter puts the real key in its place), and `headers` are extra headers that API needs forwarded. Connection headers, cookies and `x-quarter-*` are never forwarded.
- **`QUARTER_COST_CENTRES`:** optional. The cost centre file the dashboard edits and its journals split by. The default is the ledger path plus `.centres.json`.
- **`QUARTER_FAIL_OPEN`:** optional. `model` or `off` (the default). See If the ledger is down.
- **`QUARTER_MAX_WAIT`:** optional. The longest `x-quarter-wait` in whole seconds, 1 to 300 (the default). Set it under a proxy's own timeout: Cloudflare cuts requests at about 125 seconds, so `docs/HOSTED.md` sets 110.
- **`QUARTER_CLIENT_IP_HEADER`:** optional. The header a reverse proxy puts the caller's address in (`x-forwarded-for` behind Caddy, `cf-connecting-ip` behind Cloudflare). The lockout after 10 wrong keys and the audit log then go by the caller, not the proxy. Set it only when the gateway is reachable through that proxy alone, since a caller who reaches it directly can send any address.
- **`QUARTER_FREE_DECISIONS`:** optional, for a hosted free tier. The decisions a UTC month includes: each model call the gateway decides and each `POST /authorize`, card swipe and bill. A retry with `x-quarter-approval` is not counted again. Past the limit every call is still decided as before. Each reply then carries `x-quarter-free-limit: exceeded`, the month gets one `free_limit` audit row, and `status` and the dashboard say so. Unset, nothing is counted.
- **`QUARTER_REGISTER`:** optional. `on`, `shadow` or `off` (the default). See Agent register and quarterly review.
- **`QUARTER_RULES`:** optional. What each agent may do on each upstream. See Keep credentials away from agents.
- **`QUARTER_MERCURY_TOKEN`, `QUARTER_MERCURY_ACCOUNT`:** optional. Your Mercury API token and account id, to pay bills. See Bill pay.
- **`QUARTER_MERCURY_READ_TOKEN`, `QUARTER_RAMP_CLIENT_ID`, `QUARTER_RAMP_CLIENT_SECRET`:** optional. Read-only access to your agent card feeds. See Agent cards on Mercury and Ramp.
- **`QUARTER_ORG`:** optional. Your company's domain, named in spend mandates. See Spend mandates.
- **`QUARTER_STRIPE_SECRET`:** optional. The `whsec_` signing secret for Quarter cards on Stripe Issuing. See One policy for every way agents pay.
- **`QUARTER_STRIPE_APPROVALS_SECRET`:** optional. The `whsec_` signing secret of the Stripe destination for approval request events. See Approval record.

## How costs are counted

- **The hold:** every request byte counts as an input token (a token is at least one byte), plus `max_tokens` of output (or `max_completion_tokens`, `max_output_tokens`; 4096 if the call doesn't say). Per-request vendors use the flat price.
- **The charge:** the vendor's reported usage, in OpenAI or Anthropic form, from the response or the stream. OpenAI-style streamed chat calls are asked to include usage.
- **Cached input:** cache reads and writes are charged at the vendor's own cache prices (Anthropic 5-minute and 1-hour writes, OpenAI 30-minute and 12-hour writes). A model without cache prices is charged the input price for them. A write whose lifetime the vendor does not report pays the longer lifetime's price.
- **Free endpoints:** `count_tokens`, OpenAI `responses/input_tokens` and `GET /v1/models` pass through with no price, hold or charge.
- **When the vendor reports no usage:** the full hold is charged. Spend is never undercounted.
- **A failed vendor call** costs nothing.
- **An interrupted call** is charged in full.
- **Vendor connections:** kept open between calls, up to 8 idle per vendor host for 30 seconds, so most calls skip a new TLS handshake. A call is sent again only when a reused connection turns out closed before any byte of the call was written. Once the request is written, a lost reply is never retried, since the vendor may have done the work: the agent gets 502 and the call is charged in full. `HTTPS_PROXY` is honoured as before.
- **Running at the same time:** checking caps and placing the hold happen in one database write, so parallel calls can't overspend.

## Not built yet

- Creating Quarter cards, or holding the money they spend. Quarter decides swipes on cards the company issues itself through Stripe Issuing. Tested against Stripe's documented event shapes, not yet against a Stripe sandbox account.
- Direct QuickBooks, Xero and NetSuite sync over their APIs (import files only)
- Bill pay: reading invoices from PDFs or email (an agent or person enters the fields); banks other than Mercury (pay by hand and record it); following a Mercury payment after it is sent, so one that later fails or is reversed is still marked paid; filing 1099s, checking a TIN with the IRS, or backup withholding (tax-1099 is the worklist)
- Agent cards: Mercury's sign on `amount` is not documented, so its size is used and the kind says charge or refund; non-USD Ramp entities; a purpose field on `/authorize`
- Agent wallets: chains other than Base, assets other than USDC, x402 paid through Permit2 (the wallet policy refuses it), smart-account user operations in the wallet policy, and pushing the policy to CDP (it is printed only)
- Postgres (SQLite for now)
- Per-minute pricing for voice
- The dashboard's per-agent breakdown of wildcard caps

## Licence

The gateway and ledger are under the Functional Source License 1.1 with an Apache 2.0 future licence (`LICENSE.md`). You may read, run, change and self-host it for your own company. You may not offer it to others as a competing product or service. Each version becomes Apache 2.0 two years after it is released. `sellers/` and `x402/` are Apache 2.0 now.
