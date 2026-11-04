import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withX402Client } from 'agents/x402';
import { quarterOnPaymentRequired } from './cloudflare-agents.mjs';
import { fakeQuarter, required, PAYEE } from './fakes.mjs';

const accepts = (...over) => (over.length ? over : [{}]).map((o) => required(o).accepts[0]);
const SELLER = '0x00000000000000000000000000000000000000aa'; // viem signs only to a real address
const receipt = { success: true, transaction: '0xtx', network: 'eip155:8453', payer: '0xAgent' };

async function setup(answer, options = {}) {
  const quarter = await fakeQuarter(answer);
  const errors = [];
  const pay = quarterOnPaymentRequired({ url: quarter.url, key: 'qk_agent', agent: 'researcher', onError: (e) => errors.push(e.message), ...options });
  return { quarter, pay, errors };
}

test('an allowed payment is approved and settled with its transaction', async () => {
  const { quarter, pay } = await setup();
  try {
    assert.equal(await pay(accepts()), true);
    const [authorize] = quarter.seen;
    assert.deepEqual(authorize.body, { rail: 'x402', payee: PAYEE, amount_micros: 250000, network: 'eip155:8453' });
    assert.equal(authorize.headers['x-quarter-key'], 'qk_agent');
    await pay.settle({ content: [], _meta: { 'x402/payment-response': receipt } });
    const [settle] = quarter.settles();
    assert.deepEqual([settle.call_id, settle.amount_micros, settle.tx], ['c1', 250000, '0xtx']);
  } finally {
    quarter.close();
  }
});

test('a cap, a pause or a denied approval refuses the payment and says why', async () => {
  for (const [status, body] of [[402, { error: 'cap_reached' }], [423, { error: 'paused' }], [403, { error: 'approval_denied', approval_id: 'ap1' }]]) {
    const { quarter, pay, errors } = await setup(() => [status, body]);
    try {
      assert.equal(await pay(accepts()), false);
      assert.match(errors[0], new RegExp(`Quarter refused: ${body.error}`));
      await pay.settle();
      assert.deepEqual(quarter.settles(), []);
    } finally {
      quarter.close();
    }
  }
});

test('a payment that needs approval waits for it, or is refused at once without wait', async () => {
  const answer = (r) => (r.headers['x-quarter-wait'] === '30' ? [200, { call_id: 'c2' }] : [403, { error: 'approval_required', approval_id: 'ap1' }]);
  const waiting = await setup(answer, { wait: 30 });
  const now = await setup(answer);
  try {
    assert.equal(await waiting.pay(accepts()), true);
    assert.equal(await now.pay(accepts()), false);
    assert.match(now.errors[0], /approval_required, approval ap1/);
  } finally {
    waiting.quarter.close();
    now.quarter.close();
  }
});

test('Quarter unreachable refuses the payment', async () => {
  const errors = [];
  const pay = quarterOnPaymentRequired({ url: 'http://127.0.0.1:1', onError: (e) => errors.push(e.message) });
  assert.equal(await pay(accepts()), false);
  assert.match(errors[0], /Quarter could not be reached/);
});

test('options the client could pick differently, or not in USDC, are refused before Quarter is asked', async () => {
  const { quarter, pay } = await setup();
  try {
    assert.equal(await pay(accepts({}, { payTo: '0xOther' })), false);
    assert.equal(await pay(accepts({}, { amount: '900000' })), false);
    assert.equal(await pay(accepts({ asset: '0xNotUsdc' })), false);
    assert.equal(await pay(accepts({ scheme: 'upto' })), false);
    assert.deepEqual(quarter.seen, []);
    // The preferred network decides, as withX402Client's own policy does.
    const sepolia = { network: 'eip155:84532', asset: '0x036CbD53842c5426634e7929541eC2318f3dCF7e', amount: '900000' };
    assert.equal(await pay(accepts({}, sepolia)), false);
    const preferring = quarterOnPaymentRequired({ url: quarter.url, network: 'base-sepolia', onError: () => {} });
    assert.equal(await preferring(accepts({}, sepolia)), true);
    assert.equal(quarter.seen[0].body.amount_micros, 900000);
  } finally {
    quarter.close();
  }
});

test('a failed tool or payment settles 0, and no result at all counts in full', async () => {
  const { quarter, pay } = await setup();
  try {
    await pay(accepts());
    await pay.settle({ isError: true, content: [{ type: 'text', text: 'Failed to create payment payload' }] });
    await pay(accepts());
    await pay.settle();
    assert.deepEqual(quarter.settles().map((s) => s.amount_micros), [0, 250000]);
  } finally {
    quarter.close();
  }
});

test('a second payment before the first is settled is refused', async () => {
  const { quarter, pay, errors } = await setup();
  try {
    assert.equal(await pay(accepts()), true);
    assert.equal(await pay(accepts()), false);
    assert.match(errors[0], /one callback per tool call/);
  } finally {
    quarter.close();
  }
});

// The real withX402Client from agents 0.32.0, over a fake MCP server whose tool costs 0.25 USDC.
function mcpServer() {
  const calls = [];
  const client = {
    listTools: async () => ({ tools: [] }),
    callTool: async (params) => {
      calls.push(params);
      if (!params._meta?.['x402/payment']) {
        const options = accepts({ payTo: SELLER, extra: { name: 'USD Coin', version: '2' } });
        return { isError: true, content: [], _meta: { 'x402/error': { x402Version: 2, error: 'PAYMENT_REQUIRED', accepts: options } } };
      }
      return { content: [{ type: 'text', text: 'paid' }], _meta: { 'x402/payment-response': receipt } };
    },
  };
  return { client, calls };
}
const account = { address: '0x0000000000000000000000000000000000000001', signTypedData: async () => `0x${'11'.repeat(65)}` };

test('withX402Client pays only after Quarter allows, and the result settles it', async () => {
  const { quarter, pay } = await setup();
  const { client, calls } = mcpServer();
  try {
    const x402 = withX402Client(client, { account, network: 'base', maxPaymentValue: 1000000n });
    const result = await x402.callTool(pay, { name: 'data', arguments: {} });
    await pay.settle(result);
    assert.equal(result.content[0].text, 'paid');
    assert.equal(calls.length, 2);
    assert.equal(quarter.seen[0].body.payee, SELLER);
    assert.deepEqual(quarter.settles().map((s) => [s.amount_micros, s.tx]), [[250000, '0xtx']]);
  } finally {
    quarter.close();
  }
});

test('withX402Client does not pay when Quarter refuses', async () => {
  const { quarter, pay } = await setup(() => [402, { error: 'cap_reached' }]);
  const { client, calls } = mcpServer();
  try {
    const x402 = withX402Client(client, { account, maxPaymentValue: 1000000n });
    const result = await x402.callTool(pay, { name: 'data', arguments: {} });
    assert.equal(result.isError, true);
    assert.equal(calls.length, 1);
  } finally {
    quarter.close();
  }
});
