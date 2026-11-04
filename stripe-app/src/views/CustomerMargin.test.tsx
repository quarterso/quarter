import {render, getMockContextProps} from '@stripe/ui-extension-sdk/testing';
import {Banner, Table} from '@stripe/ui-extension-sdk/ui';

import CustomerMargin from './CustomerMargin';
import {customerMargin, loadSettings} from '../quarter';

jest.mock('../quarter', () => ({
  ...jest.requireActual('../quarter'),
  loadSettings: jest.fn(),
  customerMargin: jest.fn(),
}));
jest.mock('@stripe/ui-extension-sdk/http_client', () => ({createHttpClient: () => ({}), STRIPE_API_KEY: 'test'}));
jest.mock('stripe', () => ({__esModule: true, default: jest.fn(() => ({apps: {secrets: {}}}))}));

const settings = {url: 'https://acme.quarterfi.com', key: 'k'};
const row = (month: string, test_mode = false) =>
  ({month, customer: 'acme', revenue: 105_000_000, ai_cost: 2_000_000, margin: 103_000_000, margin_pct: 98.1, test_mode});

async function shown(mode: 'live' | 'test' = 'live') {
  const props = getMockContextProps({environment: {mode, objectContext: {id: 'cus_1', object: 'customer'}}});
  const {wrapper, update} = render(<CustomerMargin {...props} />);
  await new Promise((done) => setTimeout(done, 0));
  await update();
  return wrapper;
}

beforeEach(() => jest.clearAllMocks());

test('shows this month and last for the customer on the page', async () => {
  (loadSettings as jest.Mock).mockResolvedValue(settings);
  (customerMargin as jest.Mock).mockImplementation(async (_s, month) => row(month));
  const wrapper = await shown();
  expect(wrapper).toContainComponent(Table);
  expect(wrapper).toContainText('$103.00 (98.1%)');
  expect(wrapper).toContainText('Booked in Quarter as acme');
  expect((customerMargin as jest.Mock).mock.calls.map((c) => c[2])).toEqual(['cus_1', 'cus_1']);
  expect(wrapper).not.toContainComponent(Banner);
});

test("uses the vendor's own figures once Quarter has reconciled the month", async () => {
  (loadSettings as jest.Mock).mockResolvedValue(settings);
  (customerMargin as jest.Mock).mockImplementation(async (_s, month) =>
    ({...row(month), actual_cost: 1_000_000, actual_margin: 104_000_000}));
  const wrapper = await shown();
  expect(wrapper).toContainText('$1.00');
  expect(wrapper).toContainText('$104.00 (99.0%)');
});

test('asks to connect Quarter before any key is saved', async () => {
  (loadSettings as jest.Mock).mockResolvedValue(null);
  const wrapper = await shown();
  expect(wrapper).toContainComponent(Banner, {title: 'Connect Quarter'});
  expect(customerMargin).not.toHaveBeenCalled();
});

test("says so when Quarter's revenue is the other Stripe mode, or Quarter fails", async () => {
  (loadSettings as jest.Mock).mockResolvedValue(settings);
  (customerMargin as jest.Mock).mockImplementation(async (_s, month) => row(month, false));
  expect(await shown('test')).toContainComponent(Banner, {type: 'caution', title: 'Quarter reads live mode revenue'});
  (customerMargin as jest.Mock).mockRejectedValue(new Error('Stripe answered 404: No such customer'));
  expect(await shown()).toContainComponent(Banner, {type: 'critical', description: 'Stripe answered 404: No such customer'});
});
