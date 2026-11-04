// The real x402 client (@x402/fetch and @x402/core 2.29.0) paying a fake seller, with a fake
// Quarter that signs real spend mandates and a seller that checks them with spendMandateHook.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { generateKeyPairSync, sign } from 'node:crypto';
import { wrapFetchWithPayment, x402Client } from '@x402/fetch';
import { encodePaymentRequiredHeader, encodePaymentResponseHeader, x402HTTPResourceServer } from '@x402/core/http';
import { x402ResourceServer } from '@x402/core/server';
import { mandateFrom } from '@quarterfi/spend-mandate';
import { withQuarter } from './index.mjs';
import { companyPolicy, spendMandateHook } from './seller.mjs';
import { fakeQuarter, required, PAYEE, USDC_BASE } from './fakes.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ed25519');
const keys = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'k1' }] };
const b64 = (v) => Buffer.from(JSON.stringify(v)).toString('base64url');
let issued = 0;

function mandate(payee, amount) {
  const now = Math.floor(Date.now() / 1000);
  const claims = { v: 0, typ: 'spend-mandate', iss: 'buyer.example.com', sub: `c${++issued}`, aud: payee, rail: 'x402',
    max_amount_micros: amount, currency: 'USD', policy: null, entry: null, approval: null, iat: now, exp: now + 600 };
  const data = `${b64({ alg: 'EdDSA', typ: 'JWT', kid: 'k1' })}.${b64(claims)}`;
  return `${data}.${sign(null, Buffer.from(data), privateKey).toString('base64url')}`;
}

const adapter = (req) => ({ getHeader: (name) => req.headers[name.toLowerCase()] });

// A seller that asks for 0.25 USDC on Base and checks the mandate before it would settle.
// With extensions, its 402 answer advertises them, as an x402 route's extensions would.
async function seller(extensions) {
  const hook = spendMandateHook({ payee: PAYEE, trust: ['buyer.example.com'], keys });
  const paid = [];
  const server = createServer(async (req, res) => {
    if (!req.headers['payment-signature']) {
      return res.writeHead(402, { 'PAYMENT-REQUIRED': encodePaymentRequiredHeader({ ...required(), extensions }) }).end('{}');
    }
    const refused = await hook({ adapter: adapter(req), path: req.url, method: req.method }, {});
    if (refused) return res.writeHead(403).end(JSON.stringify({ error: refused.reason }));
    paid.push(req.headers);
    const receipt = { success: true, transaction: '0xabc', network: 'eip155:8453', payer: '0xbuyer' };
    res.writeHead(200, { 'PAYMENT-RESPONSE': encodePaymentResponseHeader(receipt) }).end('{"data":1}');
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  return { url: `http://127.0.0.1:${server.address().port}/data`, paid, close: () => server.close() };
}

function client() {
  const scheme = {
    scheme: 'exact',
    createPaymentPayload: async (x402Version) => ({ x402Version, payload: { signature: '0xsig' } }),
    findDefaultAsset: (asset) => (asset === USDC_BASE ? { asset, decimals: 6, symbol: 'USDC' } : undefined),
  };
  return new x402Client().register('eip155:8453', scheme);
}

test('the real x402 client pays with Quarter\'s approval and the seller accepts the mandate', async () => {
  const quarter = await fakeQuarter((r) => [200, { call_id: 'c1', authorisation: 'a', mandate: mandate(r.body.payee, r.body.amount_micros) }]);
  const shop = await seller();
  try {
    const pay = wrapFetchWithPayment(fetch, withQuarter(client(), { url: quarter.url, key: 'qk', agent: 'researcher' }));
    const res = await pay(shop.url);
    assert.equal(res.status, 200);
    assert.ok(shop.paid[0]['spend-mandate'] && shop.paid[0]['payment-signature']);
    assert.deepEqual(quarter.seen[0].body, {
      rail: 'x402', payee: PAYEE, amount_micros: 250000, network: 'eip155:8453', resource: 'https://api.example.com/data',
    });
    const [settle] = quarter.settles();
    assert.deepEqual([settle.call_id, settle.amount_micros, settle.tx], ['c1', 250000, '0xabc']);
  } finally {
    quarter.close();
    shop.close();
  }
});

test('the seller advertises company-policy; the real client echoes it with the mandate, and the real server accepts the echo', async () => {
  const quarter = await fakeQuarter((r) => [200, { call_id: 'c1', authorisation: 'a', mandate: mandate(r.body.payee, r.body.amount_micros) }]);
  const shop = await seller(companyPolicy({ required: true }));
  try {
    const pay = wrapFetchWithPayment(fetch, withQuarter(client(), { url: quarter.url, key: 'qk', agent: 'researcher' }));
    assert.equal((await pay(shop.url)).status, 200);
    const sent = shop.paid[0];
    const payload = JSON.parse(Buffer.from(sent['payment-signature'], 'base64').toString());
    assert.equal(mandateFrom(payload), sent['spend-mandate']);
    assert.deepEqual(payload.extensions['company-policy'].schema, companyPolicy()['company-policy'].schema);
    assert.equal(payload.extensions['company-policy'].info.required, true);
    const paymentRequired = { ...required(), extensions: companyPolicy({ required: true }) };
    assert.deepEqual(new x402ResourceServer({}).validateExtensions(paymentRequired, payload), { valid: true });
    const altered = structuredClone(payload);
    altered.extensions['company-policy'].info.required = false; // a client that changed the seller's info
    assert.equal(new x402ResourceServer({}).validateExtensions(paymentRequired, altered).invalidReason, 'extension_echo_mismatch');
  } finally {
    quarter.close();
    shop.close();
  }
});

test('a mandate in the payment alone, with no header, is accepted by the seller hook', async () => {
  const hook = spendMandateHook({ payee: PAYEE, keys });
  const payload = { x402Version: 2, accepted: { amount: '250000' }, extensions: { 'company-policy': { info: { required: true, mandate: mandate(PAYEE, 250000) } } } };
  const headers = { 'payment-signature': Buffer.from(JSON.stringify(payload)).toString('base64') };
  assert.equal(await hook({ adapter: { getHeader: (n) => headers[n.toLowerCase()] } }, {}), undefined);
});

test('a refusal from Quarter stops the real client before it pays', async () => {
  const quarter = await fakeQuarter(() => [402, { error: 'cap_reached', reason: 'agent researcher: day cap reached' }]);
  const shop = await seller();
  try {
    const pay = wrapFetchWithPayment(fetch, withQuarter(client(), { url: quarter.url }));
    await assert.rejects(pay(shop.url), /Payment creation aborted: Quarter refused: cap_reached, agent researcher: day cap reached/);
    assert.deepEqual(shop.paid, []);
  } finally {
    quarter.close();
    shop.close();
  }
});

test('a payment without a mandate is refused by the seller hook', async () => {
  const quarter = await fakeQuarter(() => [200, { call_id: 'c1', authorisation: 'a' }]);
  const shop = await seller();
  try {
    const pay = wrapFetchWithPayment(fetch, withQuarter(client(), { url: quarter.url, onError: () => {} }));
    const res = await pay(shop.url);
    assert.equal(res.status, 403);
    assert.match((await res.json()).error, /^spend_mandate_required/);
    assert.deepEqual(shop.paid, []);
  } finally {
    quarter.close();
    shop.close();
  }
});

test('the real x402HTTPResourceServer answers 403 when the hook refuses', async () => {
  const routes = { 'GET /data': { accepts: { scheme: 'exact', network: 'eip155:8453', payTo: PAYEE, price: '$0.25' } } };
  const http = new x402HTTPResourceServer(new x402ResourceServer({}), routes);
  http.onProtectedRequest(spendMandateHook({ payee: PAYEE, keys }));
  const headers = { 'payment-signature': Buffer.from(JSON.stringify({ accepted: { amount: '250000' } })).toString('base64') };
  const result = await http.processHTTPRequest({ adapter: { getHeader: (n) => headers[n.toLowerCase()] }, path: '/data', method: 'GET' });
  assert.equal(result.type, 'payment-error');
  assert.equal(result.response.status, 403);
  assert.match(result.response.body.error, /^spend_mandate_required/);
});
