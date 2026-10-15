/**
 * Default test run: offline and deterministic. Suites that need live vendor
 * APIs or a `.creds.json` file are listed in tests/jest/integrationSuites.js
 * and run with `npm run test:integration` instead.
 */
import { integrationTests } from './tests/jest/integrationSuites.js';

/** @type {import('ts-jest').JestConfigWithTsJest} **/
export default {
  testEnvironment: 'node',
  transform: {
    // env.ts uses top-level await, which ts-jest's CommonJS output can't
    // compile. See tests/jest/envTransformer.cjs.
    '/src/utils/env\\.ts$': '<rootDir>/tests/jest/envTransformer.cjs',
    '^.+.tsx?$': ['ts-jest', {}],
  },
  testPathIgnorePatterns: ['/node_modules/', ...integrationTests],
  testTimeout: 30000, // Set default timeout to 30 seconds
};
