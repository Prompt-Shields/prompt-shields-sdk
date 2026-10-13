/** @type {import('ts-jest').JestConfigWithTsJest} **/
export default {
  testEnvironment: 'node',
  transform: {
    // env.ts uses top-level await, which ts-jest's CommonJS output can't
    // compile. See tests/jest/envTransformer.cjs.
    '/src/utils/env\\.ts$': '<rootDir>/tests/jest/envTransformer.cjs',
    '^.+.tsx?$': ['ts-jest', {}],
  },
  testTimeout: 30000, // Set default timeout to 30 seconds
};
