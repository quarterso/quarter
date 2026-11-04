import type { PaymentRequirements } from '@x402/core/types';
import type { QuarterOptions } from './index.js';

export interface QuarterAgentsOptions extends QuarterOptions {
  /** The network given to withX402Client, when one was. Its options are the ones the client pays. */
  network?: string;
  /** Told why a payment was refused, or could not be settled in Quarter. Default console.error. */
  onError?: (error: Error) => void;
}

export interface QuarterPaymentCallback {
  (accepts: PaymentRequirements[]): Promise<boolean>;
  /** Settles the payment in Quarter from the tool's result. Pass nothing when callTool threw. */
  settle(result?: { isError?: boolean; _meta?: Record<string, unknown> }): Promise<void>;
}

/**
 * The confirmation callback for callTool on a withX402Client client. True when Quarter allows the
 * payment, false when it refuses, cannot be reached or the approval is not given in time.
 * Make one per tool call, and settle it with the result.
 */
export function quarterOnPaymentRequired(options: QuarterAgentsOptions): QuarterPaymentCallback;
