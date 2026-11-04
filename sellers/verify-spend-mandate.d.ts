/** The claims of a version 0 spend mandate. See https://quarterfi.com/spend-mandates */
export interface SpendMandateClaims {
  v: 0;
  typ: 'spend-mandate';
  /** The paying company, by its domain. */
  iss: string;
  /** Unique per mandate: accept one payment for it. */
  sub: string;
  /** The payee: a domain, wallet address or account. */
  aud: string;
  rail: string;
  /** The most the agent may pay, in millionths of a dollar. */
  max_amount_micros: number;
  currency: string;
  policy: string | null;
  entry: string | null;
  approval: string | null;
  iat: number;
  exp: number;
}

export interface KeySet {
  keys: { kty: string; crv?: string; x?: string; kid?: string; alg?: string; use?: string }[];
}

export const KEYS_PATH: '/.well-known/spend-mandate-keys.json';

/** The x402 v2 extension key the mandate travels under. */
export const EXTENSION: 'company-policy';

/** The company-policy extension, to put in PaymentRequired.extensions or an x402 route's extensions. */
export function companyPolicy(options?: { required?: boolean }): {
  'company-policy': { info: { required: boolean }; schema: Record<string, unknown> };
};

/** The mandate in an x402 v2 payment (the PaymentPayload or its base64 PAYMENT-SIGNATURE header), or null. */
export function mandateFrom(payment: string | object | null | undefined): string | null;

/** The key set a company publishes at https://<domain>/.well-known/spend-mandate-keys.json. Cached five minutes. */
export function fetchKeys(domain: string): Promise<KeySet>;

/**
 * The claims if the mandate is signed by its company, unexpired, for this payee and covers the amount.
 * Null otherwise. Throws when the company's keys cannot be fetched.
 */
export function verifySpendMandate(
  token: string | null | undefined,
  options?: { payee?: string; amountMicros?: number; keys?: KeySet; now?: number },
): Promise<SpendMandateClaims | null>;

/** Remembers mandate ids until they expire, in this process. Returns false for one already seen. */
export function memorySeen(): (id: string, exp: number) => Promise<boolean>;

/**
 * One check in front of a Fetch-style handler. `refused` is the Response to return, or null.
 * Reads the mandate from the x402 payment, else from the deprecated Spend-Mandate header.
 */
export function spendMandateGuard(options: {
  payee: string;
  trust?: string[];
  required?: boolean;
  keys?: KeySet;
  seen?: (id: string, exp: number) => Promise<boolean>;
  now?: number;
}): (request: Request, amountMicros: number) => Promise<{ claims: SpendMandateClaims | null; refused: Response | null }>;
