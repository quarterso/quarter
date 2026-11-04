# A Cloudflare Worker seller

A minimal Worker that sells one JSON answer over x402 and checks the buyer's spend mandate before it settles. It uses the verifier in `sellers/` by a relative path, so it needs no package.

## What it answers

| Answer | When |
| --- | --- |
| 402 with x402 v2 payment requirements | No payment, a payment that cannot be read, or settlement failed. |
| 403 `spend_mandate_invalid` | The mandate is forged, expired, for another payee, or under the price. |
| 409 `spend_mandate_used` | The mandate already paid once. |
| 503 `spend_mandate_unchecked` | The buyer's keys could not be fetched. |
| 200 with `PAYMENT-RESPONSE` | The payment settled. `paid_by` names the paying company, or is null. |

## Mandates are optional

A seller does not have to require a mandate. The 402 answer asks for one with `companyPolicy({ required: false })`, and a payment without one is settled and served like any x402 payment. A mandate that is sent is always checked, and a bad one is refused before anything is settled.

## Settings

`wrangler.jsonc` holds placeholders only: `PAY_TO`, `PRICE_MICROS` (USDC micro-dollars), `NETWORK`, `ASSET` and `FACILITATOR_URL`. Payment goes to the facilitator's `POST /settle`. The Worker needs `nodejs_compat`.

Each mandate is accepted once per Worker isolate. With more than one, back `seen` with a shared store such as KV or a Durable Object.

## Tests

From this folder, `node --test`. The tests use the published test vectors in `sellers/test-vectors.json` and a fake facilitator, so nothing is settled.
