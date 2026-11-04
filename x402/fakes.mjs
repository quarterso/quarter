import { createServer } from 'node:http';

export const USDC_BASE = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913';
export const PAYEE = '0xSeller';

// Quarter's /authorize and /settle, answering what `answer` says and keeping each request.
export async function fakeQuarter(answer = () => [200, { call_id: 'c1', authorisation: 'a', mandate: 'm.a.ndate' }]) {
  const seen = [];
  const server = createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      const r = { path: req.url, headers: req.headers, body: JSON.parse(raw || '{}') };
      seen.push(r);
      const [status, body] = r.path === '/settle' ? [200, { settled: true }] : answer(r);
      res.writeHead(status, { 'content-type': 'application/json' }).end(JSON.stringify(body));
    });
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const url = `http://127.0.0.1:${server.address().port}`;
  return { url, seen, close: () => server.close(), settles: () => seen.filter((r) => r.path === '/settle').map((r) => r.body) };
}

// The hook contract of x402Client in @x402/core 2.29.0 (src/client/x402Client.ts): before-hooks
// get { paymentRequired, selectedRequirements } and an abort throws; after-hooks get the same
// context spread with paymentPayload; failure hooks get it with error; response hooks run in order.
// A v2 payload echoes the seller's extensions, as mergeExtensions does with no client extension.
export function fakeClient(createPayload = async (version) => ({ x402Version: version, payload: { signature: '0xsig' } })) {
  const hooks = { before: [], after: [], failure: [], response: [] };
  return {
    onBeforePaymentCreation: (h) => hooks.before.push(h),
    onAfterPaymentCreation: (h) => hooks.after.push(h),
    onPaymentCreationFailure: (h) => hooks.failure.push(h),
    onPaymentResponse: (h) => hooks.response.push(h),
    async createPaymentPayload(paymentRequired) {
      const context = { paymentRequired, selectedRequirements: paymentRequired.accepts[0] };
      for (const hook of hooks.before) {
        const result = await hook(context);
        if (result && 'abort' in result && result.abort) throw new Error(`Payment creation aborted: ${result.reason}`);
      }
      try {
        const partial = await createPayload(paymentRequired.x402Version);
        const payload = partial.x402Version === 1 ? partial : { ...partial, extensions: paymentRequired.extensions, resource: paymentRequired.resource, accepted: context.selectedRequirements };
        for (const hook of hooks.after) await hook({ ...context, paymentPayload: payload });
        return payload;
      } catch (error) {
        for (const hook of hooks.failure) await hook({ ...context, error });
        throw error;
      }
    },
    async handlePaymentResponse(ctx) {
      for (const hook of hooks.response) if ((await hook(ctx))?.recovered) return { recovered: true };
      return undefined;
    },
  };
}

// The x402HTTPClient surface the fetch and axios wrappers use.
export const fakeHttp = (client) => ({
  client,
  encodePaymentSignatureHeader: (p) => ({ [p.x402Version === 1 ? 'X-PAYMENT' : 'PAYMENT-SIGNATURE']: 'encoded' }),
});

export const required = (over = {}, version = 2) => ({
  x402Version: version,
  resource: { url: 'https://api.example.com/data' },
  accepts: [{ scheme: 'exact', network: 'eip155:8453', asset: USDC_BASE, amount: '250000', payTo: PAYEE, maxTimeoutSeconds: 60, extra: {}, ...over }],
});

// What wrapFetchWithPayment does around one paid request, with the seller's answer as given.
export async function pay(http, paymentRequired, answer = { settleResponse: { success: true, transaction: '0xtx', network: 'eip155:8453' } }) {
  const paymentPayload = await http.client.createPaymentPayload(paymentRequired);
  const headers = http.encodePaymentSignatureHeader(paymentPayload);
  if (paymentPayload.x402Version !== 1) {
    await http.client.handlePaymentResponse({ paymentPayload, requirements: paymentPayload.accepted, ...answer });
  }
  return headers;
}
