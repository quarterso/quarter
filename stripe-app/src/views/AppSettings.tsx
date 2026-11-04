import {useEffect, useState} from 'react';
import {Box, SettingsView, Spinner, TextField} from '@stripe/ui-extension-sdk/ui';
import type {ExtensionContextValue} from '@stripe/ui-extension-sdk/context';

import {loadSettings, saveSettings, type Settings} from '../quarter';

const AppSettings = (_props: ExtensionContextValue) => {
  const [saved, setSaved] = useState<Settings | null | undefined>(undefined);
  const [status, setStatus] = useState<string>();

  useEffect(() => {
    loadSettings().then(setSaved, (e: Error) => {
      setSaved(null);
      setStatus(e.message);
    });
  }, []);

  const onSave = ({url, key}: {[name: string]: string}) => {
    setStatus('Saving');
    saveSettings(url, key, Boolean(saved)).then(
      () => {
        setStatus('Saved');
        setSaved({url, key: key || saved?.key || ''});
      },
      (e: Error) => setStatus(e.message),
    );
  };

  if (saved === undefined) {
    return (
      <SettingsView>
        <Spinner />
      </SettingsView>
    );
  }
  return (
    <SettingsView onSave={onSave} statusMessage={status}>
      <Box css={{stack: 'y', gap: 'large'}}>
        <TextField
          name="url"
          label="Quarter address"
          description="Your hosted gateway, such as https://acme.quarterfi.com"
          type="url"
          defaultValue={saved?.url}
        />
        <TextField
          name="key"
          label="Read-only key"
          description={
            (saved ? 'Saved; leave empty to keep it. ' : '') +
            'A person in QUARTER_PEOPLE with role "finance" and "read_only": true. Everyone on this Stripe account can use it.'
          }
          type="password"
        />
      </Box>
    </SettingsView>
  );
};

export default AppSettings;
