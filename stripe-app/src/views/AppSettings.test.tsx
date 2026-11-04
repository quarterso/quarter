import {render, getMockContextProps} from '@stripe/ui-extension-sdk/testing';
import {SettingsView, TextField} from '@stripe/ui-extension-sdk/ui';

import AppSettings from './AppSettings';
import {loadSettings, saveSettings} from '../quarter';

jest.mock('../quarter', () => ({loadSettings: jest.fn(), saveSettings: jest.fn()}));

async function settled(wrapper: ReturnType<typeof render>) {
  await new Promise((done) => setTimeout(done, 0));
  await wrapper.update();
  return wrapper.wrapper;
}

test('a saved key is never shown again, and saving without one keeps it', async () => {
  (loadSettings as jest.Mock).mockResolvedValue({url: 'https://acme.quarterfi.com', key: 'secret'});
  (saveSettings as jest.Mock).mockResolvedValue(undefined);
  const view = render(<AppSettings {...getMockContextProps()} />);
  const wrapper = await settled(view);
  expect(wrapper).toContainComponent(TextField, {name: 'url', defaultValue: 'https://acme.quarterfi.com'});
  expect(wrapper.find(TextField, {name: 'key'})?.prop('defaultValue')).toBeUndefined();
  wrapper.find(SettingsView)!.trigger('onSave', {url: 'https://acme.quarterfi.com', key: ''});
  expect(saveSettings).toHaveBeenCalledWith('https://acme.quarterfi.com', '', true);
  expect(await settled(view)).toContainComponent(SettingsView, {statusMessage: 'Saved'});
});

test('a refused save says why', async () => {
  (loadSettings as jest.Mock).mockResolvedValue(null);
  (saveSettings as jest.Mock).mockRejectedValue(new Error('Use your hosted Quarter address'));
  const view = render(<AppSettings {...getMockContextProps()} />);
  (await settled(view)).find(SettingsView)!.trigger('onSave', {url: 'https://x.example.com', key: 'k'});
  expect(saveSettings).toHaveBeenCalledWith('https://x.example.com', 'k', false);
  expect(await settled(view)).toContainComponent(SettingsView, {statusMessage: 'Use your hosted Quarter address'});
});
