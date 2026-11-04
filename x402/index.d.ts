import type { x402Client, x402HTTPClient } from '@x402/core/client';
import type { PaymentRequirements } from '@x402/core/types';

export interface QuarterOptions {
  /** The Quarter gateway, such as http://quarter:8787. */
  url: string;
  /** The agent key, sent as x-quarter-key. Never put it in the url. */
  key?: string;
  agent?: string;
  task?: string;
  customer?: string;
  /** Seconds (at most 300) a payment that needs a person waits for the decision. Default 0: abort at once. */
  wait?: number;
  fetch?: typeof globalThis.fetch;
  /** Told when a payment could not be settled in Quarter. Default console.error. */
  onError?: (error: Error) => void;
}

/**
 * Registers Quarter on the client's payment hooks and returns the x402HTTPClient to pass to
 * wrapFetchWithPayment or the axios interceptor. Pass an x402HTTPClient to have it used as is.
 */
export function withQuarter(client: x402Client | x402HTTPClient, options: QuarterOptions): x402HTTPClient;

/** A USDC amount in micro-dollars, or null when the asset is not USDC on a known network. */
export function usdcMicros(requirements: PaymentRequirements): number | null;
