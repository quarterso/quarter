# @quarterfi/spend-mandate

Check a spend mandate: proof that a company's spend policy allowed an AI agent to pay you.

A mandate is a short token the agent sends inside its x402 payment, as the `company-policy` extension. The paying company signs it. It names the company by its domain, the payee, the most the agent may pay, and the policy that allowed it. You check it against the keys the company publishes on its own domain. No account, no one in the middle.

The format and a check you can try in the browser: https://quarterfi.com/spend-mandates

## Install

```sh
npm install @quarterfi/spend-mandate
```

No dependencies. Node 18 or later, Deno, Bun, and Cloudflare Workers with `nodejs_compat`.

## In front of an API

```js
import { spendMandateGuard } from '@quarterfi/spend-mandate';

const guard = spendMandateGuard({ payee: 'api.example.com', trust: ['buyer.example.com'] });

export default {
  async fetch(request) {
    const { claims, refused } = await guard(request, 250_000); // this call costs $0.25
    if (refused) return refused;
    // claims.iss is the paying company. Serve the call.
    return new Response('ok');
  },
};
```

| Answer | When |
| --- | --- |
| 402 `spend_mandate_required` | No mandate was sent. Pass `required: false` to let those through. |
| 403 `spend_mandate_invalid` | Forged, expired, for another payee, or under the charge. |
| 403 `spend_mandate_untrusted` | Signed, but not by a company on `trust`. Leave `trust` out to accept any. |
| 409 `spend_mandate_used` | Already accepted once. |
| 503 `spend_mandate_unchecked` | The company's keys could not be fetched. Never a silent pass. |

Each mandate is accepted once. The default memory covers one process. With several, pass `seen`, an async function `(id, exp) => boolean` backed by a shared store, that returns false for an id it already holds.

The guard reads the mandate from the x402 payment (the `PAYMENT-SIGNATURE` header) and, failing that, from a `Spend-Mandate` header. That header is deprecated and will not be read after the next release.

## The x402 extension

Ask for a mandate in your 402 answer. With an x402 server, put it in the route's `extensions`:

```js
import { companyPolicy } from '@quarterfi/spend-mandate';

const routes = { 'GET /data': { accepts: { scheme: 'exact', network: 'eip155:8453', payTo: PAY_TO, price: '$0.25' }, extensions: companyPolicy() } };
```

`companyPolicy({ required: false })` says a mandate is welcome but not needed. The client echoes the extension in `PaymentPayload.extensions` and adds `info.mandate`. It never drops or changes what you sent. `mandateFrom(payment)` reads the mandate from a payment payload or its base64 header.

## Test vectors

`test-vectors.json` holds mandates signed with a published key (RFC 8032 test 1), the time to check each at, and whether a seller must accept it. It also holds one `PaymentRequired` and one `PaymentPayload` with the extension. Use it to test another implementation.

## Just the check

```js
import { verifySpendMandate } from '@quarterfi/spend-mandate';

const claims = await verifySpendMandate(token, { payee: 'api.example.com', amountMicros: 250_000 });
if (!claims) refuse();
```

It returns null for a mandate that is forged, expired, for another payee or under the amount, and throws when the company's keys cannot be fetched.

## Status

Version 0 of the format is a draft and may change before version 1. Issued by [Quarter](https://quarterfi.com). Questions: hello@quarterfi.com.

Apache-2.0.
