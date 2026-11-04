import type { HTTPRequestContext, ProtectedRequestHook, RouteConfig } from '@x402/core/server';
import type { KeySet } from '@quarterfi/spend-mandate';

export { companyPolicy } from '@quarterfi/spend-mandate';

export interface SpendMandateHookOptions {
  /** Your own payee: the payTo address. */
  payee?: string;
  /** The company domains you accept. Leave it out to accept any company that signs. */
  trust?: string[];
  /** false lets a paid request without a mandate through. Default true. */
  required?: boolean;
  keys?: KeySet;
  seen?: (id: string, exp: number) => Promise<boolean>;
  now?: number;
  /** The price in micro-dollars. Default: the amount of the payment, which for USDC is micro-dollars. */
  amountMicros?: number | ((context: HTTPRequestContext, routeConfig: RouteConfig) => number | Promise<number>);
}

/** spendMandateGuard as an x402HTTPResourceServer onProtectedRequest hook: a refusal answers 403. */
export function spendMandateHook(options?: SpendMandateHookOptions): ProtectedRequestHook;
