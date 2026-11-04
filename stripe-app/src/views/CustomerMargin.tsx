import {useEffect, useState} from 'react';
import {
  Banner,
  Box,
  ContextView,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from '@stripe/ui-extension-sdk/ui';
import type {ExtensionContextValue} from '@stripe/ui-extension-sdk/context';

import {customerMargin, loadSettings, recentMonths, usd, type MarginRow} from '../quarter';

type State =
  | {status: 'loading'}
  | {status: 'unset'}
  | {status: 'failed'; message: string}
  | {status: 'ready'; rows: MarginRow[]};

function marginText(r: MarginRow): string {
  const margin = r.actual_margin ?? r.margin;
  return r.revenue > 0 ? `${usd(margin)} (${((margin / r.revenue) * 100).toFixed(1)}%)` : usd(margin);
}

const CustomerMargin = ({environment}: ExtensionContextValue) => {
  const customer = environment.objectContext?.id;
  const [state, setState] = useState<State>({status: 'loading'});

  useEffect(() => {
    if (!customer) return;
    let current = true;
    (async () => {
      const settings = await loadSettings();
      if (!settings) return {status: 'unset'} as const;
      const rows = await Promise.all(recentMonths(new Date()).map((m) => customerMargin(settings, m, customer)));
      return {status: 'ready', rows} as const;
    })().then(
      (next) => current && setState(next),
      (e: Error) => current && setState({status: 'failed', message: e.message}),
    );
    return () => {
      current = false;
    };
  }, [customer]);

  // Quarter reads one Stripe mode, set by its key; this page may be the other one.
  const other = state.status === 'ready' && state.rows[0].test_mode !== (environment.mode === 'test');

  return (
    <ContextView title="AI margin" description="Revenue against the AI cost Quarter recorded for this customer">
      {state.status === 'loading' && <Spinner />}
      {state.status === 'unset' && (
        <Banner
          title="Connect Quarter"
          description="Add your Quarter address and a read-only key in this app's settings."
        />
      )}
      {state.status === 'failed' && <Banner type="critical" title="Quarter could not be read" description={state.message} />}
      {state.status === 'ready' && (
        <Box css={{stack: 'y', gap: 'medium'}}>
          {other && (
            <Banner
              type="caution"
              title={`Quarter reads ${environment.mode === 'test' ? 'live' : 'test'} mode revenue`}
              description="Its revenue key is for the other mode, so this customer's revenue is not counted."
            />
          )}
          <Table>
            <TableHead>
              <TableRow>
                <TableHeaderCell>Month</TableHeaderCell>
                <TableHeaderCell align="right">Revenue</TableHeaderCell>
                <TableHeaderCell align="right">AI cost</TableHeaderCell>
                <TableHeaderCell align="right">Margin</TableHeaderCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {state.rows.map((r) => (
                <TableRow key={r.month}>
                  <TableCell>{r.month}</TableCell>
                  <TableCell align="right">{usd(r.revenue)}</TableCell>
                  <TableCell align="right">{usd(r.actual_cost ?? r.ai_cost)}</TableCell>
                  <TableCell align="right">{marginText(r)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          <Box css={{font: 'caption', color: 'secondary'}}>
            Booked in Quarter as {state.rows[0].customer}. Revenue is invoices paid in the month, excluding tax.
            AI cost is at the vendor's own figures for months Quarter has reconciled, else as Quarter recorded it.
          </Box>
        </Box>
      )}
    </ContextView>
  );
};

export default CustomerMargin;
