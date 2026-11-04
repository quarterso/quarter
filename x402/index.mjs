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

// USDC by network: 6 decimals, so an atomic amount is micro-dollars as it is. From x402's own
// default assets (@x402/evm 2.29.0 src/defaultAssets.ts) and Circle's Solana mints.
const USDC = {
  'eip155:1': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
  'eip155:8453': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
  'eip155:84532': '0x036CbD53842c5426634e7929541eC2318f3dCF7e',
  'eip155:137': '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
  'eip155:42161': '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
  'eip155:421614': '0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d',
  'eip155:43114': '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E',
  'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
  'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1': '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU',
};
// x402 v1 network names, as Quarter's payments.caip2 maps them.
const V1 = {
  base: 'eip155:8453',
  'base-sepolia': 'eip155:84532',
  ethereum: 'eip155:1',
  polygon: 'eip155:137',
  avalanche: 'eip155:43114',
  solana: 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp',
  'solana-devnet': 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1',
};

const same = (a, b) => (/^0x/i.test(a) ? String(a).toLowerCase() === String(b).toLowerCase() : a === b);

/** The amount in micro-dollars, or null when the asset is not USDC. */
export function usdcMicros(requirements) {
  const network = V1[requirements.network] ?? requirements.network;
  if (!USDC[network] || !same(requirements.asset, USDC[network])) return null;
  const amount = String(requirements.amount ?? requirements.maxAmountRequired);
  return /^\d{1,15}$/.test(amount) && Number(amount) > 0 ? Number(amount) : null;
}

export function withQuarter(client, { url, key, agent, task, customer, wait = 0, fetch = globalThis.fetch, onError = console.error } = {}) {
  if (!url) throw new Error('withQuarter needs the url of the Quarter gateway');
  const http = typeof client.encodePaymentSignatureHeader === 'function' ? client : new x402HTTPClient(client);
  const core = http === client ? client.client : client;
  const pending = new WeakMap(); // paymentRequired -> grant, between creating and the payload
  const grants = new WeakMap(); // paymentPayload -> grant, until settled
  const headers = { 'content-type': 'application/json' };
  for (const [name, value] of Object.entries({ key, agent, task, customer })) {
    if (value) headers[`x-quarter-${name}`] = String(value);
  }

  const quarter = async (path, body, extra = {}) => {
    const res = await fetch(url.replace(/\/+$/, '') + path, {
      method: 'POST',
      headers: { ...headers, ...extra },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout((Math.min(wait, 300) + 15) * 1000),
    });
    return { status: res.status, body: await res.json().catch(() => ({})) };
  };

  const settle = async (grant, amountMicros, settleResponse) => {
    const body = { call_id: grant.call_id, amount_micros: amountMicros, tx: settleResponse?.transaction ?? '' };
    if (settleResponse) body.x402_response = Buffer.from(JSON.stringify(settleResponse)).toString('base64');
    try {
      const { status, body: answer } = await quarter('/settle', body);
      if (status !== 200) onError(new Error(`Quarter did not settle ${grant.call_id}: ${status} ${answer.error ?? ''}`));
    } catch (e) {
      onError(new Error(`Quarter did not settle ${grant.call_id}: ${e.message}`));
    }
  };

  core.onBeforePaymentCreation(async ({ paymentRequired, selectedRequirements: req }) => {
    const micros = usdcMicros(req);
    if (micros === null) {
      return { abort: true, reason: `Quarter approves USDC payments only, not ${req.asset} on ${req.network}` };
    }
    // Echoing may add to the seller's info but never replace it, so a mandate already there is refused.
    const advertised = paymentRequired.extensions?.[EXTENSION];
    if (advertised !== undefined && !(isRecord(advertised) && isRecord(advertised.info) && !('mandate' in advertised.info))) {
      return { abort: true, reason: `the seller's ${EXTENSION} extension is malformed or already holds a mandate` };
    }
    let answer;
    try {
      answer = await quarter(
        '/authorize',
        { rail: 'x402', payee: req.payTo, amount_micros: micros },
        wait > 0 ? { 'x-quarter-wait': String(wait) } : {},
      );
    } catch (e) {
      return { abort: true, reason: `Quarter could not be reached: ${e.message}` };
    }
    const { status, body } = answer;
    if (status !== 200) {
      const why = [body.error ?? `HTTP ${status}`, body.reason, body.approval_id && `approval ${body.approval_id}`];
      return { abort: true, reason: `Quarter refused: ${why.filter(Boolean).join(', ')}` };
    }
    if (advertised?.info.required === true && !body.mandate) {
      await settle({ call_id: body.call_id }, 0);
      return { abort: true, reason: 'the seller requires a spend mandate and Quarter issued none: set QUARTER_ORG' };
    }
    pending.set(paymentRequired, { call_id: body.call_id, mandate: body.mandate, micros });
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
