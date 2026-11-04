import { test, mock, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync } from 'node:fs';
import worker from './worker.mjs';

const vectors = JSON.parse(readFileSync(new URL('../../sellers/test-vectors.json', import.meta.url)));
const valid = vectors.cases.find((c) => c.name === 'valid');
const tampered = vectors.cases.find((c) => c.name === 'signature changed');
const accepted = vectors.payment_payload.accepted;

// The buyer's keys are fetched from its domain; here they come from the test vectors.
const realFetch = globalThis.fetch;
const keysUrl = 'https://buyer.example.com/.well-known/spend-mandate-keys.json';

// A facilitator's /settle that succeeds and keeps each request.
let facilitator;
before(async () => {
  mock.timers.enable({ apis: ['Date'], now: valid.now * 1000 });
  globalThis.fetch = (url, init) => (String(url) === keysUrl ? Promise.resolve(Response.json(vectors.keys)) : realFetch(url, init));
  const settled = [];
  const server = createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      settled.push({ path: req.url, body: JSON.parse(raw) });
      res.writeHead(200, { 'content-type': 'application/json' })
        .end(JSON.stringify({ success: true, transaction: '0xtx', network: accepted.network, payer: '0xbuyer' }));
    });
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  facilitator = { url: `http://127.0.0.1:${server.address().port}`, settled, close: () => server.close() };
});
after(() => {
  facilitator.close();
  globalThis.fetch = realFetch;
  mock.timers.reset();
});

const env = (over = {}) => ({ PAY_TO: valid.payee, PRICE_MICROS: String(valid.amount_micros), NETWORK: accepted.network,
  ASSET: accepted.asset, FACILITATOR_URL: facilitator.url, ...over });
const paying = (payload) => new Request('https://api.example.com/data', {
  headers: payload ? { 'payment-signature': Buffer.from(JSON.stringify(payload)).toString('base64') } : {},
});
const withMandate = (token) => ({ ...vectors.payment_payload, extensions: { 'company-policy': { info: { required: false, mandate: token } } } });

test('no payment gets 402 with x402 v2 requirements and the optional company-policy extension', async () => {
  const res = await worker.fetch(paying(), env());
  assert.equal(res.status, 402);
  const body = await res.json();
  assert.equal(body.x402Version, 2);
  assert.deepEqual(body.accepts[0], { ...accepted });
  assert.equal(body.extensions['company-policy'].info.required, false);
  assert.deepEqual(JSON.parse(Buffer.from(res.headers.get('payment-required'), 'base64')), body);
});

test('a valid mandate is settled and served', async () => {
  const res = await worker.fetch(paying(vectors.payment_payload), env());
  assert.equal(res.status, 200);
  assert.equal((await res.json()).paid_by, 'buyer.example.com');
  assert.equal(JSON.parse(Buffer.from(res.headers.get('payment-response'), 'base64')).transaction, '0xtx');
  const last = facilitator.settled.at(-1);
  assert.equal(last.path, '/settle');
  assert.deepEqual(last.body.paymentPayload, vectors.payment_payload);
  assert.deepEqual(last.body.paymentRequirements, accepted);
});

test('a tampered mandate is refused before anything is settled', async () => {
  const before = facilitator.settled.length;
  const res = await worker.fetch(paying(withMandate(tampered.token)), env());
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, 'spend_mandate_invalid');
  assert.equal(facilitator.settled.length, before);
});

test('a price over what the mandate allows is refused', async () => {
  const before = facilitator.settled.length;
  const res = await worker.fetch(paying(withMandate(valid.token)), env({ PRICE_MICROS: String(valid.amount_micros + 1) }));
  assert.equal(res.status, 403);
  assert.equal((await res.json()).error, 'spend_mandate_invalid');
  assert.equal(facilitator.settled.length, before);
});

test('a payment without a mandate is served', async () => {
  const { extensions, ...payload } = vectors.payment_payload;
  const res = await worker.fetch(paying(payload), env());
  assert.equal(res.status, 200);
  assert.equal((await res.json()).paid_by, null);
  assert.deepEqual(facilitator.settled.at(-1).body.paymentPayload, payload);
});
