/**
 * PS fork (#350): context keys shared with middlewares mounted above
 * psPseudonymise (no imports, so any of them can use it).
 *
 * UNRESTORED_RESPONSE_KEY: the JSON answer's text as the provider sent it
 * (placeholders, before restoring). Layers above psPseudonymise see the
 * restored answer in c.res; the /log/stream tail (log/index.ts) reads this
 * instead, so original values only ever reach the client.
 */
export const UNRESTORED_RESPONSE_KEY = 'psPseudonymiseUnrestored';
