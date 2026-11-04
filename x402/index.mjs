// Quarter's spend approval inside the official x402 client (@x402/core 2.x).
//
//   const http = withQuarter(client, { url: 'http://quarter:8780', key: process.env.QUARTER_KEY, agent: 'researcher' });
//   const pay = wrapFetchWithPayment(fetch, http);   // or withPaymentInterceptor(axios, http)
//
// Before each payment Quarter is asked (POST /authorize); its refusal aborts the payment. An x402
// v2 payment carries the spend mandate in its extensions, as extensions['company-policy'].info.mandate:
// the seller's advertised extension is echoed with nothing dropped or changed, and the mandate added.
// The Spend-Mandate header is still sent for sellers on @quarterfi/spend-mandate 0.1.0; it is
// deprecated and goes in the next release. After the payment, it is settled in Quarter's ledger
// (POST /settle), at 0 when it did not go through.
import { x402HTTPClient } from '@x402/core/client';
import { gateway, usdcMicros } from './quarter.mjs';

export { usdcMicros };

const EXTENSION = 'company-policy';
// The same schema as companyPolicy() in @quarterfi/spend-mandate, used when the seller did not
// advertise the extension.
const SCHEMA = {
  $schema: 'https://json-schema.org/draft/2020-12/schema',
  type: 'object',
  properties: {
    required: { type: 'boolean' },
    mandate: { type: 'string', pattern: '^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$' },
  },
  required: ['required'],
};
const isRecord = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

export function withQuarter(client, { url, key, agent, task, customer, wait = 0, fetch = globalThis.fetch, onError = console.error } = {}) {
  if (!url) throw new Error('withQuarter needs the url of the Quarter gateway');
  const http = typeof client.encodePaymentSignatureHeader === 'function' ? client : new x402HTTPClient(client);
  const core = http === client ? client.client : client;
  const pending = new WeakMap(); // paymentRequired -> grant, between creating and the payload
  const grants = new WeakMap(); // paymentPayload -> grant, until settled
  const { authorize, settle } = gateway({ url, key, agent, task, customer, wait, fetch, onError });

  core.onBeforePaymentCreation(async ({ paymentRequired, selectedRequirements: req }) => {
    // Echoing may add to the seller's info but never replace it, so a mandate already there is refused.
    const advertised = paymentRequired.extensions?.[EXTENSION];
    if (advertised !== undefined && !(isRecord(advertised) && isRecord(advertised.info) && !('mandate' in advertised.info))) {
      return { abort: true, reason: `the seller's ${EXTENSION} extension is malformed or already holds a mandate` };
    }
    const grant = await authorize(req, paymentRequired.resource?.url ?? req.resource);
    if (grant.refused) return { abort: true, reason: grant.refused };
    if (advertised?.info.required === true && !grant.mandate) {
      await settle(grant, 0);
      return { abort: true, reason: 'the seller requires a spend mandate and Quarter issued none: set QUARTER_ORG' };
    }
    pending.set(paymentRequired, grant);
  });

  core.onAfterPaymentCreation(async ({ paymentRequired, paymentPayload }) => {
    const grant = pending.get(paymentRequired);
    pending.delete(paymentRequired);
    if (!grant) return;
    if (grant.mandate && paymentPayload.x402Version !== 1) {
      const echoed = paymentPayload.extensions?.[EXTENSION] ?? { info: { required: false }, schema: SCHEMA };
      paymentPayload.extensions = {
        ...paymentPayload.extensions,
        [EXTENSION]: { ...echoed, info: { ...echoed.info, mandate: grant.mandate } },
      };
    }
    // x402 v1 reports no result to the client, so the payment counts in full once signed.
    if (paymentPayload.x402Version === 1) {
      await settle(grant, grant.micros);
      grant.settled = true;
    }
    grants.set(paymentPayload, grant);
  });

  core.onPaymentCreationFailure(async ({ paymentRequired }) => {
    const grant = pending.get(paymentRequired);
    pending.delete(paymentRequired);
    if (grant) await settle(grant, 0); // nothing was signed: release the hold
  });

  core.onPaymentResponse(async ({ paymentPayload, settleResponse, paymentRequired, error }) => {
    const grant = grants.get(paymentPayload);
    if (!grant || grant.settled) return;
    grants.delete(paymentPayload);
    if (settleResponse?.success) {
      const settled = /^\d+$/.test(settleResponse.amount ?? '') ? Number(settleResponse.amount) : grant.micros;
      return void (await settle(grant, settled, settleResponse));
    }
    if (settleResponse || paymentRequired || error) return void (await settle(grant, 0, settleResponse));
    await settle(grant, grant.micros); // no receipt either way: count it, so spend is never undercounted
  });

  const encode = http.encodePaymentSignatureHeader.bind(http);
  http.encodePaymentSignatureHeader = (paymentPayload) => {
    const out = encode(paymentPayload);
    const mandate = grants.get(paymentPayload)?.mandate;
    return mandate ? { ...out, 'Spend-Mandate': mandate } : out; // deprecated: the extension carries it
  };
  return http;
}
