// The spend-mandate check as an x402 onProtectedRequest hook (@x402/core 2.x), for sellers.
//
//   httpServer.onProtectedRequest(spendMandateHook({ payee: PAY_TO, trust: ['example.com'] }));
//
// A request without a payment goes on to x402's 402 answer, so the agent can ask its company
// for a mandate. A paid request without a valid mandate is refused with 403 before it is
// verified or settled, and the reason names what was wrong. The mandate is read from the
// payment's company-policy extension; advertise it in the route's extensions with companyPolicy().
import { spendMandateGuard } from '@quarterfi/spend-mandate';

export { companyPolicy } from '@quarterfi/spend-mandate';

const header = (adapter, name) => adapter.getHeader(name) || adapter.getHeader(name.toUpperCase());

function paidAmount(payment) {
  try {
    const amount = JSON.parse(Buffer.from(payment, 'base64').toString()).accepted?.amount;
    return /^\d{1,15}$/.test(amount ?? '') ? Number(amount) : undefined;
  } catch {
    return undefined;
  }
}

export function spendMandateHook({ amountMicros, ...options } = {}) {
  const guard = spendMandateGuard(options);
  return async (context, routeConfig) => {
    const payment = header(context.adapter, 'payment-signature');
    if (!payment) return undefined;
    const amount = typeof amountMicros === 'function' ? await amountMicros(context, routeConfig) : (amountMicros ?? paidAmount(payment));
    if (!Number.isSafeInteger(amount)) return { abort: true, reason: 'spend_mandate_unchecked: no amount to check it against' };
    const request = { headers: { get: (name) => header(context.adapter, name) ?? null } };
    const { refused } = await guard(request, amount);
    if (!refused) return undefined;
    const { error, reason } = await refused.json();
    return { abort: true, reason: `${error}: ${reason}` };
  };
}
