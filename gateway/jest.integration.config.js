/**
 * Live-API suites only (see tests/jest/integrationSuites.js). Needs network
 * access and the vendors' `.creds.json` files or env vars:
 *   npm run test:integration
 */
import base from './jest.config.js';
import { integrationTests } from './tests/jest/integrationSuites.js';

/** @type {import('ts-jest').JestConfigWithTsJest} **/
export default {
  ...base,
  testPathIgnorePatterns: ['/node_modules/'],
  testRegex: integrationTests,
};
