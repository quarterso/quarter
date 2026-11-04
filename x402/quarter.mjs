// Quarter's /authorize and /settle, shared by the x402 client hooks and the Cloudflare Agents adapter.

// USDC by network: 6 decimals, so an atomic amount is micro-dollars as it is. From x402's own
// default assets (@x402/evm 2.29.0 src/defaultAssets.ts) and Circle's Solana mints.
const USDC = {
  'eip155:1': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
  'eip155:8453': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
  'eip155:84532': '0x036CbD53842c5426634e7929541eC2318f3dCF7e',
  'eip155:137': '0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359',
  'eip155:42161': '0xaf88d065e77c8cC2239327C5EDb3A432268e5831',
  'eip155:421614': '0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d',
  'eip155:43114': '0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E',
  'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp': 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v',
  'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1': '4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU',
};
// x402 v1 network names, as Quarter's payments.caip2 maps them.
const V1 = {
  base: 'eip155:8453',
  'base-sepolia': 'eip155:84532',
  ethereum: 'eip155:1',
  polygon: 'eip155:137',
  avalanche: 'eip155:43114',
  solana: 'solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp',
  'solana-devnet': 'solana:EtWTRABZaYq6iMfeYKouRu166VU2xqa1',
};

/** A network as CAIP-2, such as eip155:8453 for base. */
export const caip2 = (network) => V1[network] ?? network;

const same = (a, b) => (/^0x/i.test(a) ? String(a).toLowerCase() === String(b).toLowerCase() : a === b);

/** The amount in micro-dollars, or null when the asset is not USDC. */
export function usdcMicros(requirements) {
  const network = caip2(requirements.network);
  if (!USDC[network] || !same(requirements.asset, USDC[network])) return null;
  const amount = String(requirements.amount ?? requirements.maxAmountRequired);
  return /^\d{1,15}$/.test(amount) && Number(amount) > 0 ? Number(amount) : null;
}

export function gateway({ url, key, agent, task, customer, wait = 0, fetch = globalThis.fetch, onError = console.error }) {
  const headers = { 'content-type': 'application/json' };
  for (const [name, value] of Object.entries({ key, agent, task, customer })) {
    if (value) headers[`x-quarter-${name}`] = String(value);
  }

  const quarter = async (path, body, extra = {}) => {
    const res = await fetch(url.replace(/\/+$/, '') + path, {
      method: 'POST',
      headers: { ...headers, ...extra },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout((Math.min(wait, 300) + 15) * 1000),
    });
    return { status: res.status, body: await res.json().catch(() => ({})) };
  };

  // The grant ({ call_id, mandate, micros }) or { refused: reason }. Every failure refuses.
  const authorize = async (req, resource = req.resource) => {
    const micros = usdcMicros(req);
    if (micros === null) return { refused: `Quarter approves USDC payments only, not ${req.asset} on ${req.network}` };
    let answer;
    try {
      answer = await quarter(
        '/authorize',
        { rail: 'x402', payee: req.payTo, amount_micros: micros, network: caip2(req.network), resource },
        wait > 0 ? { 'x-quarter-wait': String(wait) } : {},
      );
    } catch (e) {
      return { refused: `Quarter could not be reached: ${e.message}` };
    }
    const { status, body } = answer;
    if (status !== 200) {
      const why = [body.error ?? `HTTP ${status}`, body.reason, body.approval_id && `approval ${body.approval_id}`];
      return { refused: `Quarter refused: ${why.filter(Boolean).join(', ')}` };
    }
    return { call_id: body.call_id, mandate: body.mandate, micros };
  };

  const settle = async (grant, amountMicros, settleResponse) => {
    const body = { call_id: grant.call_id, amount_micros: amountMicros, tx: settleResponse?.transaction ?? '' };
    if (settleResponse) body.x402_response = Buffer.from(JSON.stringify(settleResponse)).toString('base64');
    try {
      const { status, body: answer } = await quarter('/settle', body);
      if (status !== 200) onError(new Error(`Quarter did not settle ${grant.call_id}: ${status} ${answer.error ?? ''}`));
    } catch (e) {
      onError(new Error(`Quarter did not settle ${grant.call_id}: ${e.message}`));
    }
  };

  return { authorize, settle };
}
