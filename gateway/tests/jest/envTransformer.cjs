/**
 * Jest transformer for `src/utils/env.ts` only.
 *
 * `env.ts` loads `path` and `fs` with a top-level `await import(...)` so the
 * same module runs on Node and on Cloudflare Workers. ts-jest compiles to
 * CommonJS, where top-level await is not allowed, so every suite that imported
 * `env.ts` (directly or transitively) failed to compile.
 *
 * Jest always runs on Node, so rewriting `await import('x')` to `require('x')`
 * is equivalent there. Everything else goes to ts-jest unchanged, so tests
 * still exercise the real module and its environment variable list.
 */
const tsJest = require('ts-jest').default;

const TOP_LEVEL_AWAIT_IMPORT = /await import\((['"][^'"]+['"])\)/g;
const rewrite = (source) =>
  source.replace(TOP_LEVEL_AWAIT_IMPORT, 'require($1)');

const transformer = tsJest.createTransformer();

module.exports = {
  process(source, filePath, options) {
    return transformer.process(rewrite(source), filePath, options);
  },
  getCacheKey(source, filePath, options) {
    return transformer.getCacheKey(rewrite(source), filePath, options);
  },
};
