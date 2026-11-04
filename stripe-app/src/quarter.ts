import {createHttpClient, STRIPE_API_KEY} from '@stripe/ui-extension-sdk/http_client';
import Stripe from 'stripe';

// Quarter works out margin; this app only shows it, so revenue is defined in one place.
export type MarginRow = {
  month: string;
  customer: string;
  revenue: number; // micro-dollars, as Quarter keeps them
  ai_cost: number;
  margin: number;
  margin_pct: number | null;
  test_mode: boolean;
  // At the vendor's own figures, once `quarter-ledger close` has reconciled the month.
  actual_cost?: number;
  actual_margin?: number;
};

export type Settings = {url: string; key: string};

// The hosts the manifest lets the app reach; a self-hosted gateway elsewhere cannot be.
export const GATEWAY = /^https:\/\/[a-z0-9-]+\.quarterfi\.com$/;

// Account scope: one gateway per Stripe account, readable by every Dashboard user of it,
// which is why the key must be a read-only one.
const scope = {type: 'account' as const};
const stripe = new Stripe(STRIPE_API_KEY, {httpClient: createHttpClient() as Stripe.HttpClient});

async function secret(name: string): Promise<string | null> {
  try {
    const found = await stripe.apps.secrets.find({scope, name, expand: ['payload']});
    return found.payload ?? null;
  } catch (e) {
    if ((e as {code?: string}).code === 'resource_missing') return null;
    throw e;
  }
}

export async function loadSettings(): Promise<Settings | null> {
  const [url, key] = await Promise.all([secret('quarter_url'), secret('quarter_key')]);
  return url && key ? {url, key} : null;
}

/** Saves the address, and the key unless it is left empty, which keeps the saved one. */
export async function saveSettings(url: string, key: string, hasKey: boolean): Promise<void> {
  const gateway = url.trim().replace(/\/+$/, '');
  if (!GATEWAY.test(gateway)) {
    throw new Error('Use your hosted Quarter address, such as https://acme.quarterfi.com');
  }
  if (!key.trim() && !hasKey) throw new Error('Paste a read-only Quarter key');
  await stripe.apps.secrets.create({scope, name: 'quarter_url', payload: gateway});
  if (key.trim()) await stripe.apps.secrets.create({scope, name: 'quarter_key', payload: key.trim()});
}

export async function customerMargin(settings: Settings, month: string, customer: string): Promise<MarginRow> {
  const query = new URLSearchParams({month, customer});
  const res = await fetch(`${settings.url}/margin?${query}`, {headers: {'x-quarter-key': settings.key}});
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const error = typeof body.error === 'string' ? body.error : body.error?.message;
    throw new Error(error ?? `Quarter answered ${res.status}`);
  }
  return body as MarginRow;
}

/** This month and the one before, as YYYY-MM in UTC, the way Quarter books them. */
export function recentMonths(now: Date): [string, string] {
  const month = (d: Date) => d.toISOString().slice(0, 7);
  return [month(now), month(new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - 1, 1)))];
}

export function usd(micros: number): string {
  return (micros / 1e6).toLocaleString('en-US', {style: 'currency', currency: 'USD'});
}
