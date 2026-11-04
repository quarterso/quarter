# Quarter AI Margin (Stripe App)

Shows each customer's revenue, AI cost and margin for this month and last, on the customer's page in the Stripe Dashboard.

Quarter works out the margin (`GET /margin` on your gateway). The app only shows it, so the Dashboard and the app never disagree. The app's only Stripe permission is `secret_write`, for keeping its settings in Stripe's Secret Store. It reads no Stripe data itself.

## Set up

1. Serve Quarter with `STRIPE_REVENUE_KEY` (see the main README). Use an `rk_test_` key while testing in a Stripe sandbox, and `rk_live_` for live mode.
2. Add a read-only person to `QUARTER_PEOPLE`: `{"Stripe app": {"key": "...", "role": "finance", "read_only": true}}`. Everyone with access to the Stripe account can use this key, so it can read but never approve, record or sign.
3. In the app's settings, enter your hosted address (`https://<name>.quarterfi.com`) and that key.

A Stripe customer is matched the way Quarter books revenue: its `quarter_customer` metadata, else its name, else its id. Set `quarter_customer` to the name your agents send as `x-quarter-customer`.

Only hosted gateways on `quarterfi.com` can be reached. Stripe Apps can call only the hosts their manifest lists.

## Develop

```sh
npm ci
npm test
npm run typecheck
stripe apps start    # needs the Stripe CLI, logged in to a test account
stripe apps upload   # needs the Stripe CLI, logged in to Quarter's Stripe account
```
