// Check a spend mandate: proof that a company's spend policy authorised a payment to you.
// No dependencies. Node 18 or later.
//
//   import { mandateFrom, verifySpendMandate } from './verify-spend-mandate.mjs';
//   const token = mandateFrom(req.headers['payment-signature']) ?? req.headers['spend-mandate'];
//   const claims = await verifySpendMandate(token, { payee: PAY_TO, amountMicros });
//   if (!claims) refuse();  // claims.iss is the company's domain; accept one payment per claims.sub
//
// With x402 v2 the mandate travels in the payment itself, as the company-policy extension:
// PaymentPayload.extensions['company-policy'].info.mandate. Advertise it in PaymentRequired
// with companyPolicy(). The Spend-Mandate header is still read, but is deprecated and goes
// in the next release.
//
// Returns null for a mandate that is forged, expired, for another payee or under the amount.
// spendMandateGuard, at the end, puts all of it in front of a request handler in one call.
// Throws when the company's keys cannot be fetched, so you can tell "invalid" from "could not check".
import { createPublicKey, verify } from 'node:crypto';

export const KEYS_PATH = '/.well-known/spend-mandate-keys.json';
export const EXTENSION = 'company-policy';
const DOMAIN = /^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
const cache = new Map();

const decode = (part) => JSON.parse(Buffer.from(part, 'base64url').toString());

const samePayee = (a, b) =>
  /^0x/i.test(a) && /^0x/i.test(b) ? a.toLowerCase() === b.toLowerCase() : a === b;

// What a seller puts in PaymentRequired.extensions (or an x402 route's extensions) to ask for a
// mandate. The client echoes it in PaymentPayload.extensions and adds info.mandate.
export function companyPolicy({ required = true } = {}) {
  const schema = {
    $schema: 'https://json-schema.org/draft/2020-12/schema',
    type: 'object',
    properties: {
      required: { type: 'boolean' },
      mandate: { type: 'string', pattern: '^[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+$' },
    },
    required: ['required'],
  };
  return { [EXTENSION]: { info: { required }, schema } };
}

// The mandate in an x402 v2 payment: the PaymentPayload, or its base64 PAYMENT-SIGNATURE header.
export function mandateFrom(payment) {
  try {
    const payload = typeof payment === 'string' ? JSON.parse(Buffer.from(payment, 'base64').toString()) : payment;
    const mandate = payload?.extensions?.[EXTENSION]?.info?.mandate;
    return typeof mandate === 'string' ? mandate : null;
  } catch {
    return null;
  }
}

export async function fetchKeys(domain) {
  if (!DOMAIN.test(domain ?? '')) throw new Error(`not a domain: ${domain}`);
  const hit = cache.get(domain);
  if (hit && hit.until > Date.now()) return hit.keys;
  const res = await fetch(`https://${domain}${KEYS_PATH}`, { redirect: 'error', signal: AbortSignal.timeout(3000) });
  if (!res.ok) throw new Error(`${domain} answered ${res.status} for its keys`);
  const keys = await res.json();
  cache.set(domain, { keys, until: Date.now() + 5 * 60 * 1000 });
  return keys;
}

export async function verifySpendMandate(token, { payee, amountMicros, keys, now = Date.now() / 1000 } = {}) {
  const [head, body, sig] = String(token ?? '').split('.');
  let header, claims;
  try {
    header = decode(head);
    claims = decode(body);
  } catch {
    return null;
  }
  if (!sig || header?.alg !== 'EdDSA' || claims?.typ !== 'spend-mandate' || claims.v !== 0) return null;
  const jwk = (keys ?? (await fetchKeys(claims.iss))).keys?.find(
    (k) => k.kid === header.kid && k.kty === 'OKP' && k.crv === 'Ed25519',
  );
  if (!jwk) return null;
  const key = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: jwk.x }, format: 'jwk' });
  if (!verify(null, Buffer.from(`${head}.${body}`), key, Buffer.from(sig, 'base64url'))) return null;
  if (!(claims.exp > now) || claims.iat > now + 60) return null;
  if (payee !== undefined && !samePayee(String(claims.aud), payee)) return null;
  if (amountMicros !== undefined && !(amountMicros <= claims.max_amount_micros)) return null;
  return claims;
}

// Remembers each mandate until it expires, so one cannot pay twice. In memory, so it covers
// one process; with several, pass `seen` backed by a shared store (KV, Redis, a database).
export function memorySeen() {
  const used = new Map();
  return async (id, exp) => {
    const now = Date.now() / 1000;
    for (const [k, until] of used) if (until <= now) used.delete(k);
    if (used.has(id)) return false;
    used.set(id, exp);
    return true;
  };
}

const refuse = (status, error, reason) =>
  new Response(JSON.stringify({ error, reason }), { status, headers: { 'content-type': 'application/json' } });

// In front of a Fetch-style handler (Cloudflare Workers, Hono, Next.js route handlers, Deno, Bun):
//
//   const guard = spendMandateGuard({ payee: PAY_TO, trust: ['example.com'] });
//   const { claims, refused } = await guard(request, priceMicros);
//   if (refused) return refused;
//
// It reads the mandate from the x402 payment (the PAYMENT-SIGNATURE header), and else from
// the deprecated Spend-Mandate header.
// trust: the company domains you accept, or leave it out to accept any company that signs.
// required: false lets calls without a mandate through (claims is then null).
export function spendMandateGuard({ payee, trust, required = true, keys, seen = memorySeen(), now } = {}) {
  const trusted = trust && new Set(trust.map((d) => d.toLowerCase()));
  return async (request, amountMicros) => {
    const token = mandateFrom(request.headers.get('payment-signature')) ?? request.headers.get('spend-mandate');
    if (!token) {
      return required
        ? { claims: null, refused: refuse(402, 'spend_mandate_required', `send the mandate in the ${EXTENSION} extension of the payment`) }
        : { claims: null, refused: null };
    }
    let claims;
    try {
      claims = await verifySpendMandate(token, { payee, amountMicros, keys, now });
    } catch (e) {
      return { claims: null, refused: refuse(503, 'spend_mandate_unchecked', String(e.message ?? e)) };
    }
    if (!claims) return { claims: null, refused: refuse(403, 'spend_mandate_invalid', 'forged, expired, or for another payee or amount') };
    if (trusted && !trusted.has(String(claims.iss).toLowerCase())) {
      return { claims: null, refused: refuse(403, 'spend_mandate_untrusted', `${claims.iss} is not on the trust list`) };
    }
    if (!(await seen(`${claims.iss} ${claims.sub}`, claims.exp))) {
      return { claims: null, refused: refuse(409, 'spend_mandate_used', 'this mandate already paid once') };
    }
    return { claims, refused: null };
  };
}
