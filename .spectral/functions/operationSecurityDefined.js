// Spectral custom function: every operation must have a `security` array of
// its own, or the document must have a non-empty top-level `security`.
// An operation with `security: []` is accepted: it is explicitly public.
const METHODS = ['get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace'];

export default (doc) => {
  const results = [];
  if (!doc || typeof doc !== 'object' || !doc.paths) return results;
  const rootSecured = Array.isArray(doc.security) && doc.security.length > 0;
  for (const [path, item] of Object.entries(doc.paths)) {
    if (!item || typeof item !== 'object') continue;
    for (const method of METHODS) {
      const op = item[method];
      if (!op || typeof op !== 'object') continue;
      if (Array.isArray(op.security) || rootSecured) continue;
      results.push({
        message: `${method.toUpperCase()} ${path} has no security requirement (set \`security\`, or \`security: []\` for a public route).`,
        path: ['paths', path, method],
      });
    }
  }
  return results;
};
