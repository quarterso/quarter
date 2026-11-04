import { test } from 'node:test';
import assert from 'node:assert/strict';
import { companyPolicy } from '@quarterfi/spend-mandate';
import { withQuarter, usdcMicros } from './index.mjs';
import { fakeClient, fakeHttp, fakeQuarter, pay, required, PAYEE } from './fakes.mjs';

const silent = () => {};

async function setup(answer, options = {}, createPayload) {
  const quarter = await fakeQuarter(answer);
  const http = withQuarter(fakeHttp(fakeClient(createPayload)), { url: quarter.url, key: 'qk_agent', agent: 'researcher', onError: silent, ...options });
  return { quarter, http };
}

test('an approved payment carries the mandate and is settled with its transaction', async () => {
  const { quarter, http } = await setup(undefined, { task: 't1', customer: 'acme' });
  try {
    const headers = await pay(http, required());
    assert.deepEqual(headers, { 'PAYMENT-SIGNATURE': 'encoded', 'Spend-Mandate': 'm.a.ndate' });
    const [authorize] = quarter.seen;
    assert.equal(authorize.path, '/authorize');
    assert.deepEqual(authorize.body, {
      rail: 'x402', payee: PAYEE, amount_micros: 250000, network: 'eip155:8453', resource: 'https://api.example.com/data',
    });
    assert.equal(authorize.headers['x-quarter-key'], 'qk_agent');
    assert.equal(authorize.headers['x-quarter-agent'], 'researcher');
    assert.equal(authorize.headers['x-quarter-task'], 't1');
    assert.equal(authorize.headers['x-quarter-customer'], 'acme');
    assert.equal(authorize.headers['x-quarter-wait'], undefined);
    const [settle] = quarter.settles();
    assert.equal(settle.call_id, 'c1');
    assert.equal(settle.amount_micros, 250000);
    assert.equal(settle.tx, '0xtx');
    assert.equal(JSON.parse(Buffer.from(settle.x402_response, 'base64')).transaction, '0xtx');
  } finally {
    quarter.close();
  }
});

test('the mandate travels in the company-policy extension, beside the deprecated header', async () => {
  const { quarter, http } = await setup();
  try {
    const payload = await http.client.createPaymentPayload(required());
    const { schema } = companyPolicy()['company-policy'];
    assert.deepEqual(payload.extensions, { 'company-policy': { info: { required: false, mandate: 'm.a.ndate' }, schema } });
    assert.equal(http.encodePaymentSignatureHeader(payload)['Spend-Mandate'], 'm.a.ndate');
  } finally {
    quarter.close();
  }
});

test('the seller\'s extensions are echoed whole, and only the mandate is added', async () => {
  const { quarter, http } = await setup();
  try {
    const extensions = {
      'company-policy': { info: { required: true, note: 'from the seller' }, schema: { type: 'object', title: 'theirs' } },
      'payment-identifier': { info: { required: false }, schema: { type: 'object' } },
    };
    const advertised = structuredClone(extensions);
    const payload = await http.client.createPaymentPayload({ ...required(), extensions });
    assert.deepEqual(extensions, advertised); // the seller's own object is not changed
    assert.deepEqual(payload.extensions, {
      ...advertised,
      'company-policy': { ...advertised['company-policy'], info: { required: true, note: 'from the seller', mandate: 'm.a.ndate' } },
    });
  } finally {
    quarter.close();
  }
});

test('a seller extension that already holds a mandate, or is malformed, is refused before Quarter is asked', async () => {
  const { quarter, http } = await setup();
  try {
    for (const ext of [{ info: { required: false, mandate: 'x.y.z' }, schema: {} }, { info: 'yes' }, null]) {
      const asked = { ...required(), extensions: { 'company-policy': ext } };
      await assert.rejects(http.client.createPaymentPayload(asked), /company-policy extension is malformed or already holds a mandate/);
    }
    assert.deepEqual(quarter.seen, []);
  } finally {
    quarter.close();
  }
});

test('a seller that requires a mandate Quarter did not issue: aborted, and the hold released', async () => {
  const { quarter, http } = await setup(() => [200, { call_id: 'c3', authorisation: 'a' }]);
  try {
    const asked = { ...required(), extensions: companyPolicy({ required: true }) };
    await assert.rejects(http.client.createPaymentPayload(asked), /requires a spend mandate and Quarter issued none/);
    assert.deepEqual(quarter.settles().map((s) => [s.call_id, s.amount_micros]), [['c3', 0]]);
  } finally {
    quarter.close();
  }
});

test('a settled amount below the authorised one is what is charged', async () => {
  const { quarter, http } = await setup();
  try {
    await pay(http, required(), { settleResponse: { success: true, transaction: '0xtx', network: 'eip155:8453', amount: '90000' } });
    assert.equal(quarter.settles()[0].amount_micros, 90000);
  } finally {
    quarter.close();
  }
});

test('a cap, an approval and a pause abort the payment with Quarter\'s reason', async () => {
  const answers = {
    402: { error: 'cap_reached', reason: 'agent researcher: day cap reached' },
    403: { error: 'approval_required', reason: 'over the approval line', approval_id: 'ap1' },
    423: { error: 'agent_paused', reason: 'agent paused: runaway' },
  };
  for (const [status, body] of Object.entries(answers)) {
    const { quarter, http } = await setup(() => [Number(status), body]);
    try {
      await assert.rejects(pay(http, required()), (e) => {
        assert.match(e.message, /^Payment creation aborted: Quarter refused: /);
        assert.ok(e.message.includes(body.error) && e.message.includes(body.reason));
        if (body.approval_id) assert.ok(e.message.includes('approval ap1'));
        return true;
      });
      assert.deepEqual(quarter.settles(), []);
    } finally {
      quarter.close();
    }
  }
});

test('wait is sent so a payment that needs a person waits for the decision', async () => {
  const { quarter, http } = await setup(undefined, { wait: 120 });
  try {
    await pay(http, required());
    assert.equal(quarter.seen[0].headers['x-quarter-wait'], '120');
  } finally {
    quarter.close();
  }
});

test('a payment that did not go through settles 0, so the hold is released', async () => {
  const failures = [
    { settleResponse: { success: false, errorReason: 'insufficient_funds', transaction: '', network: 'eip155:8453' } },
    { paymentRequired: required() }, // verify failed: the seller answered 402 again
    { error: new Error('socket hang up') },
  ];
  for (const answer of failures) {
    const { quarter, http } = await setup();
    try {
      await pay(http, required(), answer);
      assert.equal(quarter.settles()[0].amount_micros, 0);
    } finally {
      quarter.close();
    }
  }
});

test('a payload that cannot be created releases the hold', async () => {
  const { quarter, http } = await setup(undefined, {}, async () => {
    throw new Error('wallet locked');
  });
  try {
    await assert.rejects(pay(http, required()), /wallet locked/);
    assert.deepEqual(quarter.settles().map((s) => s.amount_micros), [0]);
  } finally {
    quarter.close();
  }
});

test('an asset that is not USDC is refused without asking Quarter', async () => {
  const { quarter, http } = await setup();
  try {
    const weth = required({ asset: '0x4200000000000000000000000000000000000006' });
    await assert.rejects(pay(http, weth), /Quarter approves USDC payments only, not 0x4200.* on eip155:8453/);
    await assert.rejects(pay(http, required({ network: 'eip155:999999' })), /USDC payments only/);
    assert.deepEqual(quarter.seen, []);
  } finally {
    quarter.close();
  }
});

test('Quarter unreachable aborts the payment', async () => {
  const http = withQuarter(fakeHttp(fakeClient()), { url: 'http://127.0.0.1:1', onError: silent });
  await assert.rejects(pay(http, required()), /Quarter could not be reached/);
});

test('no mandate configured: the payment goes without the header', async () => {
  const { quarter, http } = await setup(() => [200, { call_id: 'c2', authorisation: 'a' }]);
  try {
    assert.deepEqual(await pay(http, required()), { 'PAYMENT-SIGNATURE': 'encoded' });
    assert.equal((await http.client.createPaymentPayload(required())).extensions, undefined);
  } finally {
    quarter.close();
  }
});

test('x402 v1 has no result hook, so the payment counts in full once signed', async () => {
  const { quarter, http } = await setup();
  try {
    const v1 = required({ network: 'base', amount: undefined, maxAmountRequired: '10000' }, 1);
    assert.deepEqual(await pay(http, v1), { 'X-PAYMENT': 'encoded', 'Spend-Mandate': 'm.a.ndate' });
    assert.deepEqual(quarter.settles().map((s) => s.amount_micros), [10000]);
  } finally {
    quarter.close();
  }
});

test('usdcMicros reads atomic USDC as micro-dollars', () => {
  assert.equal(usdcMicros(required().accepts[0]), 250000);
  assert.equal(usdcMicros(required({ asset: '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913' }).accepts[0]), 250000);
  assert.equal(usdcMicros(required({ amount: '1.5' }).accepts[0]), null);
  assert.equal(usdcMicros(required({ amount: '0' }).accepts[0]), null);
});
