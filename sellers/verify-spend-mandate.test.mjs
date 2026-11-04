import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { companyPolicy, mandateFrom, spendMandateGuard, verifySpendMandate } from './verify-spend-mandate.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ed25519');
const kid = 'k1';
const keys = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid }] };
const b64 = (v) => Buffer.from(JSON.stringify(v)).toString('base64url');
const now = Math.floor(Date.now() / 1000);

function mandate(over = {}) {
  const claims = { v: 0, typ: 'spend-mandate', iss: 'buyer.example.com', sub: 'c1', aud: 'api.example.com', rail: 'x402',
    max_amount_micros: 500_000, currency: 'USD', policy: null, entry: null, approval: null, iat: now, exp: now + 600, ...over };
  const data = `${b64({ alg: 'EdDSA', typ: 'JWT', kid })}.${b64(claims)}`;
  return `${data}.${sign(null, Buffer.from(data), privateKey).toString('base64url')}`;
}
// Change one signature character well inside it: the last ones can hold only padding bits.
const tampered = (token) => {
  const i = token.length - 20;
  return token.slice(0, i) + (token[i] === 'A' ? 'B' : 'A') + token.slice(i + 1);
};
const req = (token) => new Request('https://api.example.com/', { headers: token ? { 'spend-mandate': token } : {} });

test('accepts a good mandate and refuses the bad ones', async () => {
  assert.equal((await verifySpendMandate(mandate(), { payee: 'api.example.com', amountMicros: 250_000, keys })).iss, 'buyer.example.com');
  assert.equal(await verifySpendMandate(mandate(), { payee: 'other.com', keys }), null);
  assert.equal(await verifySpendMandate(mandate(), { amountMicros: 600_000, keys }), null);
  assert.equal(await verifySpendMandate(mandate({ exp: now - 1 }), { keys }), null);
  assert.equal(await verifySpendMandate(tampered(mandate()), { keys }), null);
  assert.equal(await verifySpendMandate('not a token', { keys }), null);
});

test('the guard answers 402, 403, 409 and passes a good one once', async () => {
  const guard = spendMandateGuard({ payee: 'api.example.com', trust: ['buyer.example.com'], keys });
  assert.equal((await guard(req(), 1)).refused.status, 402);
  assert.equal((await guard(req(mandate({ iss: 'else.example.com' })), 1)).refused.status, 403);
  const once = mandate({ sub: 'c2' });
  assert.equal((await guard(req(once), 1)).refused, null);
  assert.equal((await guard(req(once), 1)).refused.status, 409);
});

const vectors = JSON.parse(readFileSync(new URL('./test-vectors.json', import.meta.url)));
const paid = (payload, headers = {}) =>
  new Request('https://api.example.com/', { headers: { 'payment-signature': Buffer.from(JSON.stringify(payload)).toString('base64'), ...headers } });

test('every published test vector is decided as it says', async () => {
  for (const c of vectors.cases) {
    const claims = await verifySpendMandate(c.token, { payee: c.payee, amountMicros: c.amount_micros, keys: vectors.keys, now: c.now });
    assert.equal(claims !== null, c.valid, c.name);
  }
  assert.deepEqual(companyPolicy({ required: true }), vectors.payment_required.extensions);
  assert.equal(mandateFrom(vectors.payment_signature_header), vectors.cases[0].token);
  assert.equal(mandateFrom(vectors.payment_payload), vectors.cases[0].token);
  for (const junk of [null, undefined, 'not base64 json', {}, { extensions: { 'company-policy': { info: { mandate: 7 } } } }]) {
    assert.equal(mandateFrom(junk), null);
  }
});

test('the guard reads the mandate from the payment first, the deprecated header after', async () => {
  const guard = spendMandateGuard({ payee: 'api.example.com', keys });
  const inside = (token) => ({ x402Version: 2, extensions: { 'company-policy': { info: { required: true, mandate: token } } } });
  assert.equal((await guard(paid(inside(mandate({ sub: 'e1' }))), 1)).claims.sub, 'e1');
  const both = paid(inside(mandate({ sub: 'e2' })), { 'spend-mandate': mandate({ sub: 'e3' }) });
  assert.equal((await guard(both, 1)).claims.sub, 'e2');
  assert.equal((await guard(paid({ x402Version: 2 }, { 'spend-mandate': mandate({ sub: 'e4' }) }), 1)).claims.sub, 'e4');
  const none = await guard(paid({ x402Version: 2 }), 1);
  assert.equal(none.refused.status, 402);
  assert.match((await none.refused.json()).reason, /company-policy extension/);
});
