/**
 * Token-file variables must stay paths on Node: env.ts reads `/...` values
 * as files by default, which turned AZURE_FEDERATED_TOKEN_FILE into the
 * token itself and made the Azure workload providers readFileSync() a JWT.
 */
import { mkdtempSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

const file = join(mkdtempSync(join(tmpdir(), 'ps-env-')), 'token');
writeFileSync(file, 'eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln');

const names = [
  'AZURE_FEDERATED_TOKEN_FILE',
  'OPENAI_IDENTITY_TOKEN_FILE',
  'ANTHROPIC_IDENTITY_TOKEN_FILE',
];

describe('token file variables (Node)', () => {
  const saved: Record<string, string | undefined> = {};
  beforeAll(() => {
    for (const n of names) {
      saved[n] = process.env[n];
      process.env[n] = file;
    }
  });
  afterAll(() => {
    for (const n of names) {
      if (saved[n] === undefined) delete process.env[n];
      else process.env[n] = saved[n];
    }
  });

  it.each(names)('%s is kept as the path, not the contents', async (n) => {
    // Loaded after the variables are set: env.ts reads them once.
    const { Environment } = await import('./env');
    expect(Environment()[n]).toBe(file);
  });
});
