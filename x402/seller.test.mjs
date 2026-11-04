import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync, sign } from 'node:crypto';
import { spendMandateHook } from './seller.mjs';

const { privateKey, publicKey } = generateKeyPairSync('ed25519');
const keys = { keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'k1' }] };
const b64 = (v) => Buffer.from(JSON.stringify(v)).toString('base64url');
const now = Math.floor(Date.now() / 1000);

function mandate(over = {}) {
  const claims = { v: 0, typ: 'spend-mandate', iss: 'buyer.example.com', sub: 's1', aud: '0xSeller', rail: 'x402',
    max_amount_micros: 500_000, currency: 'USD', policy: null, entry: null, approval: null, iat: now, exp: now + 600, ...over };
  const data = `${b64({ alg: 'EdDSA', typ: 'JWT', kid: 'k1' })}.${b64(claims)}`;
  return `${data}.${sign(null, Buffer.from(data), privateKey).toString('base64url')}`;
}
const payment = (amount) => Buffer.from(JSON.stringify({ x402Version: 2, accepted: { amount } })).toString('base64');
const context = (headers) => ({ adapter: { getHeader: (n) => headers[n] } });

test('an unpaid request goes on to the 402 answer', async () => {
  assert.equal(await spendMandateHook({ payee: '0xSeller', keys })(context({}), {}), undefined);
});

test('a good mandate passes once; a wrong payee, a short one or a reuse is refused', async () => {
  const hook = spendMandateHook({ payee: '0xSeller', trust: ['buyer.example.com'], keys });
  const paid = (token, amount = '250000') => hook(context({ 'payment-signature': payment(amount), 'spend-mandate': token }), {});
  assert.equal(await paid(mandate()), undefined);
  assert.match((await paid(mandate())).reason, /^spend_mandate_used/);
  assert.match((await paid(mandate({ sub: 's2', aud: '0xOther' }))).reason, /^spend_mandate_invalid/);
  assert.match((await paid(mandate({ sub: 's3' }), '600000')).reason, /^spend_mandate_invalid/);
  assert.match((await paid(mandate({ sub: 's4', iss: 'else.example.com' }))).reason, /^spend_mandate_untrusted/);
});

test('the amount can come from the seller instead of the payment', async () => {
  const hook = spendMandateHook({ payee: '0xSeller', keys, amountMicros: () => 600_000 });
  const headers = { 'PAYMENT-SIGNATURE': 'not base64 json', 'SPEND-MANDATE': mandate({ sub: 's5' }) };
  assert.match((await hook(context(headers), {})).reason, /^spend_mandate_invalid/);
  const unknown = spendMandateHook({ payee: '0xSeller', keys });
  assert.match((await unknown(context(headers), {})).reason, /^spend_mandate_unchecked/);
});
