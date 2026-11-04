import uiExtensionJestConfig from '@stripe/ui-extension-tools/jest.config.ui-extension.mjs';

export default {
  ...uiExtensionJestConfig,
  // The SDK's testing helpers ship as ES modules, which Jest does not run untransformed.
  transform: {...uiExtensionJestConfig.transform, '^.+\\.m?jsx?$': 'ts-jest'},
  transformIgnorePatterns: ['/node_modules/(?!(@stripe/ui-extension-sdk|@remote-ui)/)'],
};
