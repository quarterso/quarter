// A Cloudflare Worker that sells one JSON answer over x402 and checks the buyer's spend mandate
// before it settles. A mandate is optional: a payment without one is served as any x402 payment.
import { companyPolicy, memorySeen, spendMandateGuard } from '../../sellers/verify-spend-mandate.mjs';

const seen = memorySeen();

const json = (status, body, headers = {}) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json', ...headers } });
const encode = (value) => Buffer.from(JSON.stringify(value)).toString('base64');

function requirements(env) {
  return {
    scheme: 'exact',
    network: env.NETWORK,
    amount: env.PRICE_MICROS,
    asset: env.ASSET,
    payTo: env.PAY_TO,
    maxTimeoutSeconds: 60,
    extra: { name: 'USDC', version: '2' },
  };
}

function paymentRequired(request, env, error) {
  const body = {
    x402Version: 2,
    error,
    resource: { url: request.url, description: 'One paid JSON answer', mimeType: 'application/json' },
    accepts: [requirements(env)],
    extensions: companyPolicy({ required: false }),
  };
  return json(402, body, { 'PAYMENT-REQUIRED': encode(body) });
}

export default {
  async fetch(request, env) {
    const header = request.headers.get('payment-signature');
    if (!header) return paymentRequired(request, env, 'Payment required');
    let payment;
    try {
      payment = JSON.parse(Buffer.from(header, 'base64').toString());
    } catch {
      return paymentRequired(request, env, 'The payment could not be read');
    }

    const guard = spendMandateGuard({ payee: env.PAY_TO, required: false, seen });
    const { claims, refused } = await guard(request, Number(env.PRICE_MICROS));
    if (refused) return refused;

    const res = await fetch(`${env.FACILITATOR_URL}/settle`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ x402Version: 2, paymentPayload: payment, paymentRequirements: requirements(env) }),
    });
    const receipt = res.ok ? await res.json() : { success: false, errorReason: `facilitator answered ${res.status}` };
    if (!receipt.success) return paymentRequired(request, env, receipt.errorReason ?? 'Settlement failed');

    return json(200, { data: 'the paid answer', paid_by: claims?.iss ?? null }, { 'PAYMENT-RESPONSE': encode(receipt) });
  },
};
