# @quarterfi/x402-spend-control

Quarter's spend approval inside the official x402 client. Every x402 payment your agent makes is approved by your Quarter gateway before it is signed, carries a spend mandate the seller can check, and is settled in Quarter's ledger afterwards. The agent pays exactly as before.

Quarter never holds keys or money. Your agent's own wallet signs and pays.

## Install

```sh
npm install @quarterfi/x402-spend-control
```

It needs `@x402/core` 2.29 or later in the 2.x line, which `@x402/fetch`, `@x402/axios` and `@x402/mcp` already bring. It has no other dependencies.

## Use

```js
import { withQuarter } from '@quarterfi/x402-spend-control';

const http = withQuarter(client, { url: 'http://quarter:8780', key: process.env.QUARTER_AGENT_KEY, agent: 'researcher' });
const pay = wrapFetchWithPayment(fetch, http);
```

`client` is the `x402Client` you already built, with its schemes registered. Pass the returned client where you passed `client` before: to `wrapFetchWithPayment`, or to the axios interceptor. For MCP, keep passing `client` to `x402MCPClient`: the hooks are on it, so paid tool calls are approved and settled too.

Options: `task` and `customer` are sent with each payment, like the gateway's other `x-quarter-*` headers. `wait` is how many seconds (at most 300) a payment that needs a person waits for the decision. `onError` is told when a payment could not be settled in Quarter; by default it goes to `console.error`. The agent key goes in a header, never in a URL or a log.

## What Quarter's answer does to the payment

Before the payment is signed, the package calls `POST /authorize` with the payee and the amount in micro-dollars.

- **200:** the payment goes ahead. When your company has set `QUARTER_ORG`, an x402 version 2 payment carries the spend mandate in its `company-policy` extension. If the seller advertised that extension, it is echoed as sent, with the mandate added; one that already holds a mandate is refused before Quarter is asked. A seller that requires a mandate when Quarter issued none gets no payment, and the hold is released. The `Spend-Mandate` header is also sent for one release. It is deprecated.
- **402, over a cap:** the payment is aborted with Quarter's reason. Nothing is signed.
- **403, needs approval:** with `wait` set, the payment waits for the person. Approved, it goes ahead; denied, or still waiting when `wait` ends, it is aborted. The reason names the approval id.
- **423, agent or task paused:** the payment is aborted.
- **Quarter cannot be reached, or any other answer:** the payment is aborted. A spend control that fails open is not one.

The error the x402 client throws then includes `Payment creation aborted: Quarter refused: <error>, <reason>`.

After the paid request, the package calls `POST /settle`:

- The seller settled: Quarter records the amount settled and the transaction.
- The seller answered 402 again, or settlement failed: it settles 0, so the hold is released.
- No receipt at all: it settles the full amount, so spend is never undercounted.
- The wallet could not sign: it settles 0.

Only USDC is approved, since Quarter counts in dollars: on Base, Ethereum, Polygon, Arbitrum, Avalanche and Solana, and their test networks Base Sepolia, Arbitrum Sepolia and Solana devnet. A payment in another asset is aborted before Quarter is asked.

If the paid request itself throws (the network failed), x402 runs no hook, so the hold stays open and counts against caps until it is settled with `POST /settle`. x402 version 1 tells the client nothing after the payment, so a version 1 payment is settled in full as soon as it is signed. MCP tool calls carry no headers, but the mandate is in the payment's extensions, which MCP sends in `_meta["x402/payment"]`. The seller hook below checks HTTP requests only; an MCP seller reads the mandate with `mandateFrom` from `@quarterfi/spend-mandate`. A version 1 payment has no extensions and carries the mandate only in the header.

## Cloudflare Agents SDK

`withX402Client` from `agents/x402` asks a confirmation callback before each paid tool call. Make that callback Quarter:

```js
import { quarterOnPaymentRequired } from '@quarterfi/x402-spend-control/cloudflare-agents';

const pay = quarterOnPaymentRequired({ url: 'http://quarter:8780', key: env.QUARTER_AGENT_KEY, agent: 'researcher', network: 'base' });
const result = await this.x402Client.callTool(pay, { name, arguments: args });
await pay.settle(result);
```

It takes the same options as `withQuarter`, plus `network` when you gave one to `withX402Client`. Make one callback per tool call.

- The callback returns true when Quarter allows the payment. A cap, a pause, a denied approval, an approval not given within `wait`, or Quarter not answering returns false, and the tool call ends with "User declined payment". `onError` is told why.
- The callback sees every way the tool can be paid, before the client picks one. It allows only when every exact EVM option the client could pick pays the same payee the same USDC amount.
- `pay.settle(result)` records the payment: the transaction when the tool returned a receipt, 0 when the tool failed, so the seller did not settle. If `callTool` threw, call `pay.settle()`: it counts in full. Until it is settled, the hold counts against caps.
- The callback cannot change the payment, so it carries no spend mandate.

## For sellers: check the mandate in x402's own hooks

```js
import { spendMandateHook } from '@quarterfi/x402-spend-control/seller';

httpServer.onProtectedRequest(spendMandateHook({ payee: PAY_TO, trust: ['example.com'] }));
```

Ask for the mandate in the route's `extensions` with `companyPolicy()`, also exported here. This needs the `spend-mandate` package as well. A request without a payment goes on to x402's 402 answer, so the agent can get a mandate. A paid request without a valid mandate is refused with 403 before it is verified or settled; the reason says why (`spend_mandate_required`, `spend_mandate_invalid`, `spend_mandate_untrusted`, `spend_mandate_used` or `spend_mandate_unchecked`). The mandate is checked against the amount of the payment, which for USDC is micro-dollars; pass `amountMicros` to give the price yourself.

## License

Apache-2.0
