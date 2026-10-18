/**
 * Guards on the publish workflow (.github/workflows/publish-partner-ts.yml).
 *
 * The publish job holds an OIDC id-token and NPM_TOKEN, so no dependency's
 * install script may run in it: every install uses `--ignore-scripts`, and
 * our own build runs as an explicit step instead.
 */
import * as fs from 'fs';
import * as path from 'path';
import { parse } from 'yaml';

const ROOT = path.resolve(__dirname, '../../..');
const WORKFLOW = path.join(ROOT, '.github/workflows/publish-partner-ts.yml');

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Json = any;
const workflow: Json = parse(fs.readFileSync(WORKFLOW, 'utf8'));
const steps: Json[] = Object.values<Json>(workflow.jobs).flatMap((job) => job.steps ?? []);
const runs: string[] = steps.map((s) => s.run).filter((r): r is string => typeof r === 'string');

describe('publish workflow', () => {
  it('runs with an id-token (so the checks below matter)', () => {
    expect(workflow.permissions['id-token']).toBe('write');
  });

  it('installs dependencies only with --ignore-scripts', () => {
    const installs = runs.filter((r) => /\bnpm (ci|install|i)\b/.test(r));
    expect(installs.length).toBeGreaterThan(0);
    for (const r of installs) expect([r, r.includes('--ignore-scripts')]).toEqual([r, true]);
    // `npx <pkg>` could fetch and run a package that is not in the lockfile.
    for (const r of runs.filter((r) => /\bnpx\b/.test(r))) expect(r).toMatch(/^npx (jest|tsc)\b/);
  });

  it('builds explicitly before publishing', () => {
    const build = runs.findIndex((r) => r.includes('npm run build'));
    const publish = runs.findIndex((r) => r.includes('npm publish'));
    expect(build).toBeGreaterThanOrEqual(0);
    expect(publish).toBeGreaterThan(build);
  });

  it('needs no dependency install scripts (so --ignore-scripts loses nothing)', () => {
    const lock = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../package-lock.json'), 'utf8'));
    const withScripts = Object.entries<Json>(lock.packages ?? {})
      // Optional, platform-only packages (fsevents on macOS) are skipped on
      // the Linux runner anyway.
      .filter(([name, p]) => name !== '' && p.hasInstallScript && !p.optional)
      .map(([name]) => name);
    expect(withScripts).toEqual([]);
  });

  it('documents tag protection for partner-ts-v* in RELEASING.md', () => {
    const releasing = fs.readFileSync(path.resolve(__dirname, '../RELEASING.md'), 'utf8');
    expect(releasing).toMatch(/tag ruleset/i);
    expect(releasing).toContain('`partner-ts-v*`');
  });
});
