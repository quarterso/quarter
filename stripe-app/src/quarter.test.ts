import Stripe from 'stripe';

import {customerMargin, loadSettings, recentMonths, saveSettings, usd} from './quarter';

jest.mock('@stripe/ui-extension-sdk/http_client', () => ({createHttpClient: () => ({}), STRIPE_API_KEY: 'test'}));
jest.mock('stripe', () => {
  const secrets = {find: jest.fn(), create: jest.fn()};
  return {__esModule: true, default: Object.assign(jest.fn(() => ({apps: {secrets}})), {secrets})};
});
const secrets = (Stripe as unknown as {secrets: {find: jest.Mock; create: jest.Mock}}).secrets;

beforeEach(() => jest.clearAllMocks());

test('settings are unset until both are saved, and other failures are not hidden', async () => {
  const missing = Object.assign(new Error('No such secret'), {code: 'resource_missing'});
  secrets.find.mockRejectedValueOnce(missing).mockResolvedValueOnce({payload: 'qa_key'});
  expect(await loadSettings()).toBeNull();
  secrets.find.mockResolvedValueOnce({payload: 'https://acme.quarterfi.com'}).mockResolvedValueOnce({payload: 'k'});
  expect(await loadSettings()).toEqual({url: 'https://acme.quarterfi.com', key: 'k'});
  secrets.find.mockRejectedValue(new Error('rate limited'));
  await expect(loadSettings()).rejects.toThrow('rate limited');
});

test('only a hosted Quarter address is saved, and an empty key keeps the saved one', async () => {
  await expect(saveSettings('https://evil.example.com', 'k', false)).rejects.toThrow('hosted Quarter address');
  await expect(saveSettings('https://acme.quarterfi.com', ' ', false)).rejects.toThrow('read-only Quarter key');
  await saveSettings('https://acme.quarterfi.com/', '', true);
  expect(secrets.create.mock.calls).toEqual([
    [{scope: {type: 'account'}, name: 'quarter_url', payload: 'https://acme.quarterfi.com'}],
  ]);
});

test('margin is asked of Quarter with the key in a header, and its error is passed on', async () => {
  const row = {month: '2026-10', customer: 'acme', revenue: 1, ai_cost: 0, margin: 1, margin_pct: 100, test_mode: false};
  const fetchMock = jest.fn().mockResolvedValueOnce({ok: true, json: async () => row}).mockResolvedValueOnce({
    ok: false, status: 403, json: async () => ({error: 'Vic is viewer; this needs finance or above'}),
  });
  global.fetch = fetchMock;
  const settings = {url: 'https://acme.quarterfi.com', key: 'k'};
  expect(await customerMargin(settings, '2026-10', 'cus_1')).toEqual(row);
  expect(fetchMock).toHaveBeenCalledWith('https://acme.quarterfi.com/margin?month=2026-10&customer=cus_1',
    {headers: {'x-quarter-key': 'k'}});
  await expect(customerMargin(settings, '2026-10', 'cus_1')).rejects.toThrow('needs finance');
});

test('months and money read the way Quarter books them', () => {
  expect(recentMonths(new Date('2026-01-15T00:00:00Z'))).toEqual(['2026-01', '2025-12']);
  expect(usd(105_000_000)).toBe('$105.00');
  expect(usd(-2_500_000)).toBe('-$2.50');
});
