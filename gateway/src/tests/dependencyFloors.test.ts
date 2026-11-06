import { readFileSync } from 'fs';
import { join } from 'path';

/**
 * PS fork (#208): runtime dependencies the gateway serves requests with must
 * stay at or above the versions that fix the advisories `npm audit` reported
 * on 2026-11-06 (the Portkey enterprise changelog 2.29-2.33 "updated
 * dependencies to patch security vulnerabilities"). Reads the lockfile, so it
 * checks what `npm ci` installs (CI and the image), not a local node_modules.
 */
const FLOORS: Record<string, string> = {
  // GHSA-88fw-hqm2-52qc (CORS), GHSA-p77w-8qqv-26rm (cache), GHSA-9vqf-7f2p-gf9v
  // (bodyLimit), GHSA-g6gw-c38x-mqfc (parseBody) and others below 4.13.7
  hono: '4.13.7',
  // GHSA-wc8c-qw6v-h7f6, GHSA-92pp-h63x-v22m, GHSA-frvp-7c67-39w9 (serveStatic);
  // GHSA-rmxm-3fg6-px4f (serveStatic), fixed only on the 2.x line
  '@hono/node-server': '2.1.6',
  // GHSA-58qx-3vcg-4xpx (memory disclosure), GHSA-96hv-2xvq-fx4p (DoS)
  ws: '8.21.0',
};

const compare = (a: string, b: string): number => {
  const pa = a.split('.').map(Number);
  const pb = b.split('.').map(Number);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] ?? 0) !== (pb[i] ?? 0)) return (pa[i] ?? 0) - (pb[i] ?? 0);
  }
  return 0;
};

describe('runtime dependency security floors (#208)', () => {
  const lock = JSON.parse(
    readFileSync(join(__dirname, '..', '..', 'package-lock.json'), 'utf8')
  );

  it('compares versions numerically', () => {
    expect(compare('4.13.13', '4.13.7')).toBeGreaterThan(0);
    expect(compare('4.9.7', '4.13.7')).toBeLessThan(0);
    expect(compare('8.21.0', '8.21.0')).toBe(0);
  });

  it.each(Object.entries(FLOORS))('%s is at least %s', (name, floor) => {
    const entries = Object.entries(lock.packages as Record<string, any>).filter(
      ([path, entry]) =>
        path.endsWith(`node_modules/${name}`) && !entry.dev && entry.version
    );
    expect(entries.length).toBeGreaterThan(0);
    for (const [, entry] of entries) {
      expect(compare(entry.version, floor)).toBeGreaterThanOrEqual(0);
    }
  });
});
