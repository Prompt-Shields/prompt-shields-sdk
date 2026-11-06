/**
 * PS_ROUTE_POLICY_FILE (#336): validation, start-up load, reload keeping the
 * last good policy, and the router serving the loaded policy.
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { Hono } from 'hono';
import { psRouter } from './hono';
import {
  loadedRoutePolicy,
  parseRoutePolicyDoc,
  parseRoutePolicyText,
  RoutePolicyError,
  setLoadedRoutePolicy,
} from './policyDoc';
import {
  loadRoutePolicyFile,
  loadRoutePolicyFromEnv,
  reloadIntervalMs,
  RoutePolicyWatcher,
} from './policyFileLoader';
import bundledSchema from './route-policy.v1.schema.json';

const SCHEMA_DIR = path.resolve(__dirname, '../../../../schemas/route-policy');
const readJson = (p: string) => JSON.parse(fs.readFileSync(p, 'utf8'));
const EXAMPLES = fs
  .readdirSync(path.join(SCHEMA_DIR, 'examples'))
  .filter((f) => f.endsWith('.json'))
  .sort();

/** US-only candidates; confidential calls must stay in the EEA. */
function usOnlyPolicy(version = 'us-only-1'): any {
  return {
    schema_version: 'route-policy.v1',
    policy_version: version,
    default_group: 'balanced',
    quality_to_group: {
      draft: 'cheap',
      balanced: 'balanced',
      critical: 'balanced',
    },
    groups: {
      cheap: [
        {
          model: 'gpt-4o-mini',
          provider: 'openai',
          input_cost_per_1k: 0.00015,
          output_cost_per_1k: 0.0006,
          jurisdiction: 'US',
        },
      ],
      balanced: [
        {
          model: 'gpt-4o',
          provider: 'openai',
          input_cost_per_1k: 0.0025,
          output_cost_per_1k: 0.01,
          jurisdiction: 'US',
        },
      ],
    },
    sensitivity: {
      levels: ['public', 'internal', 'confidential', 'restricted'],
      unclassified_default: 'internal',
    },
    data_policy: {
      public: { jurisdictions: ['any'] },
      internal: { jurisdictions: ['any'] },
      confidential: { jurisdictions: ['EEA'] },
      restricted: { jurisdictions: ['EEA'] },
    },
  };
}

let dir: string;
let watchers: RoutePolicyWatcher[] = [];
beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'ps-route-policy-'));
  setLoadedRoutePolicy(undefined);
});
afterEach(() => {
  watchers.forEach((w) => w.stop());
  watchers = [];
  fs.rmSync(dir, { recursive: true, force: true });
  setLoadedRoutePolicy(undefined);
  jest.restoreAllMocks();
});

const writePolicy = (doc: unknown, name = 'policy.json') => {
  const p = path.join(dir, name);
  fs.writeFileSync(p, typeof doc === 'string' ? doc : JSON.stringify(doc));
  return p;
};

describe('parseRoutePolicyDoc', () => {
  it('bundles an exact copy of the shared schema', () => {
    expect(bundledSchema).toEqual(
      readJson(path.join(SCHEMA_DIR, 'route-policy.v1.schema.json'))
    );
  });

  it.each(EXAMPLES)('accepts example %s', (name) => {
    const policy = parseRoutePolicyDoc(
      readJson(path.join(SCHEMA_DIR, 'examples', name))
    );
    expect(Object.keys(policy.groups).length).toBeGreaterThan(0);
  });

  it('keeps the data policy and sensitivity', () => {
    const policy = parseRoutePolicyDoc(usOnlyPolicy());
    expect(policy.policyVersion).toBe('us-only-1');
    expect(policy.dataPolicy?.confidential).toEqual({
      jurisdictions: ['EEA'],
    });
    expect(policy.sensitivity?.unclassifiedDefault).toBe('internal');
  });

  it.each<[string, (p: any) => void, RegExp]>([
    ['schema', (p) => delete p.groups, /missing groups/],
    ['unknown key', (p) => (p.extra = 1), /\$\.extra: not allowed/],
    [
      'default_group',
      (p) => (p.default_group = 'frontier'),
      /default_group: "frontier" is not a group/,
    ],
    [
      'quality_to_group',
      (p) => (p.quality_to_group.critical = 'nope'),
      /quality_to_group\.critical/,
    ],
    [
      'data_policy level',
      (p) => (p.data_policy.secret = { jurisdictions: ['EEA'] }),
      /data_policy\.secret: "secret" is not a sensitivity level/,
    ],
    [
      'unclassified_default',
      (p) => (p.sensitivity.unclassified_default = 'top'),
      /unclassified_default/,
    ],
  ])('rejects a bad %s', (_, mutate, message) => {
    const doc = usOnlyPolicy();
    mutate(doc);
    expect(() => parseRoutePolicyDoc(doc)).toThrow(RoutePolicyError);
    expect(() => parseRoutePolicyDoc(doc)).toThrow(message);
  });

  it('checks data_policy keys against the default levels without sensitivity', () => {
    const doc = usOnlyPolicy();
    delete doc.sensitivity;
    expect(() => parseRoutePolicyDoc(doc)).not.toThrow();
    doc.data_policy.secret = { jurisdictions: ['EEA'] };
    expect(() => parseRoutePolicyDoc(doc)).toThrow(/not a sensitivity level/);
  });

  it('rejects non-JSON and non-object documents', () => {
    expect(() => parseRoutePolicyText('{nope')).toThrow(/not valid JSON/);
    expect(() => parseRoutePolicyText('[]')).toThrow(/JSON object/);
  });
});

describe('loadRoutePolicyFile', () => {
  it('does nothing without PS_ROUTE_POLICY_FILE', async () => {
    expect(await loadRoutePolicyFromEnv({})).toBeUndefined();
    expect(loadedRoutePolicy()).toBeUndefined();
  });

  it('loads a valid file and makes it active', async () => {
    const file = writePolicy(usOnlyPolicy());
    const res = await loadRoutePolicyFromEnv({ PS_ROUTE_POLICY_FILE: file });
    watchers.push(res!.watcher);
    expect(res!.policy.policyVersion).toBe('us-only-1');
    expect(loadedRoutePolicy()).toBe(res!.policy);
  });

  it.each<[string, () => string, RegExp]>([
    ['a missing file', () => path.join(dir, 'absent.json'), /ENOENT/],
    ['a directory', () => dir, /must be a file/],
    ['invalid JSON', () => writePolicy('{'), /not valid JSON/],
    [
      'an invalid policy',
      () => writePolicy({ ...usOnlyPolicy(), default_group: 'x' }),
      /not a group/,
    ],
  ])('refuses %s at start', async (_, file, message) => {
    await expect(
      loadRoutePolicyFromEnv({ PS_ROUTE_POLICY_FILE: file() })
    ).rejects.toThrow(message);
    expect(loadedRoutePolicy()).toBeUndefined();
  });

  it('reloads a changed file and keeps the last good policy on a bad one', async () => {
    const error = jest.spyOn(console, 'error').mockImplementation(() => {});
    jest.spyOn(console, 'log').mockImplementation(() => {});
    const file = writePolicy(usOnlyPolicy('v1'));
    const { watcher } = await loadRoutePolicyFile(file);
    watchers.push(watcher);

    expect(await watcher.reload()).toBe(false); // unchanged

    writePolicy(usOnlyPolicy('v2'));
    expect(await watcher.reload()).toBe(true);
    expect(loadedRoutePolicy()?.policyVersion).toBe('v2');

    writePolicy('{ broken');
    expect(await watcher.reload()).toBe(false);
    expect(loadedRoutePolicy()?.policyVersion).toBe('v2');
    expect(error).toHaveBeenCalledTimes(1);
    expect(error.mock.calls[0][0]).toMatch(/keeping the last good policy/);
    // The same bad content is reported once, not on every poll.
    expect(await watcher.reload()).toBe(false);
    expect(error).toHaveBeenCalledTimes(1);

    fs.rmSync(file);
    expect(await watcher.reload()).toBe(false);
    expect(loadedRoutePolicy()?.policyVersion).toBe('v2');

    writePolicy(usOnlyPolicy('v3'));
    expect(await watcher.reload()).toBe(true);
    expect(loadedRoutePolicy()?.policyVersion).toBe('v3');
  });

  it('reloads on SIGHUP', async () => {
    jest.spyOn(console, 'log').mockImplementation(() => {});
    const file = writePolicy(usOnlyPolicy('v1'));
    const { watcher } = await loadRoutePolicyFile(file, { sighup: true });
    watchers.push(watcher);
    writePolicy(usOnlyPolicy('v2'));
    process.emit('SIGHUP' as any);
    await watcher.reload(); // joins the reload the signal started
    expect(loadedRoutePolicy()?.policyVersion).toBe('v2');
    watcher.stop();
    expect(process.listenerCount('SIGHUP')).toBe(0);
  });

  it('parses PS_ROUTE_POLICY_RELOAD_SECONDS', () => {
    expect(reloadIntervalMs({})).toBe(0);
    expect(reloadIntervalMs({ PS_ROUTE_POLICY_RELOAD_SECONDS: '0' })).toBe(0);
    expect(reloadIntervalMs({ PS_ROUTE_POLICY_RELOAD_SECONDS: '30' })).toBe(
      30000
    );
    expect(reloadIntervalMs({ PS_ROUTE_POLICY_RELOAD_SECONDS: '0.2' })).toBe(
      1000
    );
    expect(() =>
      reloadIntervalMs({ PS_ROUTE_POLICY_RELOAD_SECONDS: 'soon' })
    ).toThrow(RoutePolicyError);
  });
});

describe('psRouter with the loaded policy', () => {
  function makeApp() {
    const app = new Hono();
    app.use('*', psRouter(loadedRoutePolicy));
    app.post('/v1/chat/completions', async (c) => {
      const body = await c.req.json();
      return c.json({ model: body.model });
    });
    return app;
  }
  const post = (app: Hono, headers: Record<string, string> = {}) =>
    app.request('/v1/chat/completions', {
      method: 'POST',
      headers: { 'content-type': 'application/json', ...headers },
      body: JSON.stringify({
        model: 'auto',
        messages: [{ role: 'user', content: 'hi' }],
      }),
    });

  it('refuses model calls until a policy is loaded', async () => {
    const res = await post(makeApp());
    expect(res.status).toBe(503);
  });

  it('enforces the file data policy and follows a reload', async () => {
    jest.spyOn(console, 'log').mockImplementation(() => {});
    const file = writePolicy(usOnlyPolicy('v1'));
    const { watcher } = await loadRoutePolicyFile(file);
    watchers.push(watcher);
    const app = makeApp();

    const internal = await post(app, {
      'x-ps-data-classification': 'internal',
    });
    expect(internal.status).toBe(200);
    expect(['gpt-4o', 'gpt-4o-mini']).toContain(
      ((await internal.json()) as any).model
    );

    const confidential = await post(app, {
      'x-ps-data-classification': 'confidential',
    });
    expect(confidential.status).toBe(403);
    expect(((await confidential.json()) as any).error.code).toBe(
      'policy_violation'
    );

    // Allow the US for confidential: the next request sees the new policy.
    const relaxed = usOnlyPolicy('v2');
    relaxed.data_policy.confidential.jurisdictions = ['any'];
    writePolicy(relaxed);
    expect(await watcher.reload()).toBe(true);
    const after = await post(app, {
      'x-ps-data-classification': 'confidential',
    });
    expect(after.status).toBe(200);
  });
});
