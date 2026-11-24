/**
 * PS fork (#340): the central policy bundle parser and the puller.
 */
import {
  activePolicyVersion,
  activeRoutePolicy,
  centralAccessPolicy,
  centralPolicy,
  parsePolicyBundle,
  PolicyBundleError,
  setCentralPolicy,
} from './state';
import {
  policyPullConfig,
  PolicyPullConfigError,
  startPolicyPull,
} from './puller';
import { withPolicyVersion } from '../ps-telemetry';
import { setLoadedRoutePolicy } from '../router/policyDoc';

const ROUTE = {
  schema_version: 'route-policy.v1',
  policy_version: 'file-1',
  default_group: 'cheap',
  quality_to_group: { draft: 'cheap', balanced: 'cheap', critical: 'cheap' },
  groups: {
    cheap: [
      {
        model: 'mistral-small',
        provider: 'mistral',
        input_cost_per_1k: 0.0002,
        output_cost_per_1k: 0.0006,
      },
    ],
  },
};

const ACCESS = {
  version: 1,
  default: 'deny',
  groups: [{ name: 'eng', match: ['engineering'], models: ['gpt-4o-mini'] }],
};

function bundle(tenantSections: Record<string, unknown>, extra: any = {}) {
  return {
    schema: 'ps-policy-bundle.v1',
    tenant: { version: 'default@2', sections: tenantSections },
    teams: {},
    projects: {},
    ...extra,
  };
}

afterEach(() => {
  setCentralPolicy(undefined);
  setLoadedRoutePolicy(undefined);
});

describe('parsePolicyBundle', () => {
  test('applies route and access sections and names the version', () => {
    const p = parsePolicyBundle(
      bundle({ route: { version: 1, policy: ROUTE }, access: ACCESS }),
      '"abc"'
    );
    expect(p.version).toBe('default@2');
    expect(p.etag).toBe('"abc"');
    // The central version replaces the document's own policy_version.
    expect(p.route?.policyVersion).toBe('default@2');
    expect(p.access?.defaultAction).toBe('deny');
    expect(p.access?.groups[0].name).toBe('eng');
    expect(p.ignored).toEqual([]);
  });

  test('an empty bundle is no policy at all', () => {
    const p = parsePolicyBundle({
      schema: 'ps-policy-bundle.v1',
      tenant: { version: null, sections: {} },
      teams: {},
      projects: {},
    });
    expect(p.version).toBeUndefined();
    expect(p.route).toBeUndefined();
    expect(p.access).toBeUndefined();
  });

  test('sections this gateway does not enforce are ignored', () => {
    const p = parsePolicyBundle(bundle({ prices: { version: 1, x: 1 } }));
    expect(p.ignored).toEqual(['prices']);
  });

  test.each([
    ['wrong schema', { ...bundle({}), schema: 'nope' }],
    ['tenant not an object', { ...bundle({}), tenant: [] }],
    ['unknown route version', bundle({ route: { version: 2, policy: ROUTE } })],
    [
      'invalid route policy',
      bundle({
        route: { version: 1, policy: { ...ROUTE, default_group: 'nope' } },
      }),
    ],
    [
      'invalid access section',
      bundle({ access: { version: 1, default: 'maybe', groups: [] } }),
    ],
    [
      'invalid team entry',
      bundle(
        {},
        {
          teams: {
            t1: {
              name: 't',
              version: 'x',
              sections: { access: { version: 1, groups: 'no' } },
            },
          },
        }
      ),
    ],
  ])('refuses the whole bundle: %s', (_name, raw) => {
    expect(() => parsePolicyBundle(raw)).toThrow(PolicyBundleError);
  });

  test('team and project entries are validated and counted', () => {
    const eff = { name: 'x', version: 'a', sections: { access: ACCESS } };
    const p = parsePolicyBundle(
      bundle({}, { teams: { t1: eff }, projects: { p1: eff, p2: eff } })
    );
    expect([p.teams, p.projects]).toEqual([1, 2]);
  });
});

describe('active policy accessors', () => {
  test('central route and version win over the file', () => {
    const file = parsePolicyBundle(
      bundle({ route: { version: 1, policy: ROUTE } })
    ).route!;
    file.policyVersion = 'file-1';
    setLoadedRoutePolicy(file);
    expect(activeRoutePolicy()).toBe(file);
    expect(activePolicyVersion()).toBe('file-1');
    expect(withPolicyVersion({} as any).policy_version).toBe('file-1');

    const central = parsePolicyBundle(
      bundle({ route: { version: 1, policy: ROUTE }, access: ACCESS })
    );
    setCentralPolicy(central);
    expect(activeRoutePolicy()).toBe(central.route);
    expect(centralAccessPolicy()).toBe(central.access);
    expect(withPolicyVersion({} as any).policy_version).toBe('default@2');
    // An event that already names a version keeps it.
    expect(
      withPolicyVersion({ policy_version: 'sdk' } as any).policy_version
    ).toBe('sdk');

    // A central policy without a route section falls back to the file.
    setCentralPolicy(parsePolicyBundle(bundle({ access: ACCESS })));
    expect(activeRoutePolicy()).toBe(file);
  });

  test('no policy anywhere: nothing is added', () => {
    expect(activeRoutePolicy()).toBeUndefined();
    expect(withPolicyVersion({} as any)).toEqual({});
  });
});

describe('policyPullConfig', () => {
  test('off by default', () => {
    expect(policyPullConfig({})).toBeUndefined();
  });

  test('defaults and the collector URL', () => {
    expect(
      policyPullConfig({
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'ps-ik-x',
        PS_COLLECTOR_URL: 'https://collector.example.com/',
      })
    ).toEqual({
      url: 'https://collector.example.com/api/v1/policy/effective',
      apiKey: 'ps-ik-x',
      intervalMs: 30_000,
    });
    expect(
      policyPullConfig({
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'ps-ik-x',
        PS_POLICY_PULL_API_KEY: 'ps-ik-policy',
        PS_POLICY_PULL_INTERVAL_SECONDS: '10',
      })
    ).toMatchObject({ apiKey: 'ps-ik-policy', intervalMs: 10_000 });
  });

  test.each([
    [{ PS_POLICY_PULL_ENABLED: 'true' }],
    [
      {
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_POLICY_PULL_INTERVAL_SECONDS: '1',
      },
    ],
    [
      {
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_POLICY_PULL_INTERVAL_SECONDS: 'soon',
      },
    ],
    [
      {
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_COLLECTOR_URL: 'http://collector.example.com',
      },
    ],
    [
      {
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_COLLECTOR_URL: 'http://10.0.0.5:8000',
      },
    ],
    [
      {
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_COLLECTOR_URL: 'ftp://collector',
      },
    ],
  ])('refuses %j', (env) => {
    expect(() => policyPullConfig(env)).toThrow(PolicyPullConfigError);
  });

  test.each([
    'http://localhost:8000',
    'http://127.0.0.1:8000',
    'http://[::1]:8000',
    'http://ps-collector:8000',
    'http://ps-collector.ps.svc:8000',
    'http://ps-collector.ps.svc.cluster.local:8000',
    'https://10.0.0.5',
  ])('plain http only to loopback / in-cluster names: %s', (url) => {
    expect(
      policyPullConfig({
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_COLLECTOR_URL: url,
      })
    ).toMatchObject({ url: `${url}/api/v1/policy/effective` });
  });

  test('PS_POLICY_PULL_ALLOW_HTTP lifts the https requirement', () => {
    expect(
      policyPullConfig({
        PS_POLICY_PULL_ENABLED: 'true',
        PS_API_KEY: 'k',
        PS_COLLECTOR_URL: 'http://10.0.0.5:8000',
        PS_POLICY_PULL_ALLOW_HTTP: 'true',
      })
    ).toMatchObject({ url: 'http://10.0.0.5:8000/api/v1/policy/effective' });
  });
});

describe('startPolicyPull', () => {
  const config = {
    url: 'https://collector.example.com/api/v1/policy/effective',
    apiKey: 'ps-ik-policy',
    intervalMs: 3_600_000,
  };
  const log = () => ({ log: jest.fn(), warn: jest.fn(), error: jest.fn() });
  const json = (body: unknown, etag: string) =>
    new Response(JSON.stringify(body), {
      status: 200,
      headers: { etag, 'content-type': 'application/json' },
    });

  test('applies, sends the ETag, keeps the last good policy on errors', async () => {
    const good = bundle({
      route: { version: 1, policy: ROUTE },
      access: ACCESS,
    });
    const calls: RequestInit[] = [];
    const responses: Array<() => Response | Promise<Response>> = [
      () => json(good, '"v1"'),
      () => new Response(null, { status: 304 }),
      () => {
        throw new Error('ECONNREFUSED');
      },
      () => new Response('oops', { status: 500 }),
      () => json(bundle({ route: { version: 9, policy: ROUTE } }), '"bad"'),
      () => json(bundle({ route: { version: 9, policy: ROUTE } }), '"bad"'),
      () => json(bundle({ access: ACCESS }), '"v2"'),
    ];
    const fetch = jest.fn(async (_url: string, init: RequestInit) => {
      calls.push(init);
      return responses.shift()!();
    });
    const l = log();
    const puller = await startPolicyPull(config, {
      fetch,
      log: l,
      oidcEnabled: false,
    });
    try {
      expect(centralPolicy()?.version).toBe('default@2');
      expect(activeRoutePolicy()?.policyVersion).toBe('default@2');
      expect((calls[0].headers as any).Authorization).toBe(
        'Bearer ps-ik-policy'
      );
      expect((calls[0].headers as any)['If-None-Match']).toBeUndefined();
      // The access section needs OIDC: said once.
      expect(l.warn).toHaveBeenCalledTimes(1);

      expect(await puller.pull()).toBe('unchanged');
      expect((calls[1].headers as any)['If-None-Match']).toBe('"v1"');
      const first = centralPolicy();

      expect(await puller.pull()).toBe('unreachable');
      expect(await puller.pull()).toBe('unreachable');
      expect(centralPolicy()).toBe(first);
      expect(l.error).toHaveBeenCalledTimes(2); // two distinct reasons

      // A malformed bundle is never applied, and is reported once.
      expect(await puller.pull()).toBe('rejected');
      expect(await puller.pull()).toBe('rejected');
      expect(centralPolicy()).toBe(first);
      expect(l.error).toHaveBeenCalledTimes(3);

      expect(await puller.pull()).toBe('updated');
      expect(centralPolicy()?.etag).toBe('"v2"');
      expect(centralPolicy()?.route).toBeUndefined();
    } finally {
      puller.stop();
    }
  });

  test('collector down at start: no central policy, local settings apply', async () => {
    const fetch = jest.fn(async () => {
      throw new Error('ECONNREFUSED');
    });
    const l = log();
    const puller = await startPolicyPull(config, { fetch, log: l });
    try {
      expect(centralPolicy()).toBeUndefined();
      expect(l.error.mock.calls[0][0]).toContain('local policy settings apply');
    } finally {
      puller.stop();
    }
  });

  test('a malformed first bundle is not applied', async () => {
    const fetch = jest.fn(
      async () => new Response('{not json', { status: 200 })
    );
    const l = log();
    const puller = await startPolicyPull(config, { fetch, log: l });
    try {
      expect(centralPolicy()).toBeUndefined();
      expect(l.error).toHaveBeenCalledTimes(1);
    } finally {
      puller.stop();
    }
  });

  test('a private collector URL needs TRUSTED_CUSTOM_HOSTS', async () => {
    const fetch = jest.fn();
    const l = log();
    const puller = await startPolicyPull(
      { ...config, url: 'http://169.254.169.254/api/v1/policy/effective' },
      { fetch, log: l }
    );
    try {
      expect(fetch).not.toHaveBeenCalled();
      expect(l.error.mock.calls[0][0]).toContain('TRUSTED_CUSTOM_HOSTS');
    } finally {
      puller.stop();
    }
  });
});
