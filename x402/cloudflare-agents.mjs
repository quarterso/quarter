// Quarter's spend approval as the confirmation callback of the Cloudflare Agents SDK (agents/x402).
//
//   const pay = quarterOnPaymentRequired({ url: 'http://quarter:8780', key: env.QUARTER_KEY, agent: 'researcher' });
//   const result = await this.x402Client.callTool(pay, { name, arguments: args });
//   await pay.settle(result);
//
// withX402Client asks the callback with every way the tool can be paid, before it picks one. Quarter
// authorises only when every option the client could pick pays the same payee the same USDC amount.
import { caip2, gateway, usdcMicros } from './quarter.mjs';

// The options withX402Client's exact EVM scheme can pay, narrowed to its preferred network if any match.
function payable(accepts, network) {
  const evm = accepts.filter((r) => r.scheme === 'exact' && caip2(r.network).startsWith('eip155:'));
  const preferred = network ? evm.filter((r) => caip2(r.network) === caip2(network)) : [];
  return preferred.length > 0 ? preferred : evm;
}

export function quarterOnPaymentRequired({ url, network, onError = console.error, ...options } = {}) {
  if (!url) throw new Error('quarterOnPaymentRequired needs the url of the Quarter gateway');
  const { authorize, settle } = gateway({ url, onError, ...options });
  let grant = null;

  const refuse = (reason) => {
    onError(new Error(`Quarter did not approve the payment: ${reason}`));
    return false;
  };

  const onPaymentRequired = async (accepts) => {
    if (grant) return refuse('the last payment is not settled yet: make one callback per tool call');
    const options = payable(accepts, network);
    if (options.length === 0) return refuse('no option is an exact payment on an EVM network');
    const [first] = options;
    const differs = options.some((r) => r.payTo !== first.payTo || usdcMicros(r) !== usdcMicros(first));
    if (differs) return refuse('the options pay different payees or amounts');
    const answer = await authorize(first);
    if (answer.refused) return refuse(answer.refused);
    grant = answer;
    return true;
  };

  // Settles the payment from the tool's result, or with no result when callTool threw.
  onPaymentRequired.settle = async (result) => {
    if (!grant) return;
    const paid = grant;
    grant = null;
    const receipt = result?._meta?.['x402/payment-response'];
    if (receipt?.success) return void (await settle(paid, paid.micros, receipt));
    if (result?.isError) return void (await settle(paid, 0, receipt)); // the seller settles only a tool that worked
    await settle(paid, paid.micros); // no receipt either way: count it, so spend is never undercounted
  };
  return onPaymentRequired;
}
