import {
  defaultPolicy,
  parseRouteHeaders,
  estimateInputTokens,
  estimateTierCost,
  HeuristicStrategy,
  resolveRoute,
  RoutePolicy,
} from './index';
import { DEFAULT_POLICY_CATALOG_VERSION } from './policy';
import generatedPolicy from './defaultPolicy.json';

// A small deterministic policy for assertions that don't need the defaults.
const policy: RoutePolicy = {
  defaultGroup: 'balanced',
  qualityToGroup: {
    draft: 'cheap',
    balanced: 'balanced',
    critical: 'frontier',
  },
  groups: {
    cheap: [
      {
        model: 'mini',
        group: 'cheap',
        inputCostPer1k: 0.0001,
        outputCostPer1k: 0.0004,
      },
    ],
    balanced: [
      {
        model: 'mid',
        group: 'balanced',
        inputCostPer1k: 0.0025,
        outputCostPer1k: 0.01,
      },
    ],
    frontier: [
      {
        model: 'big',
        group: 'frontier',
        inputCostPer1k: 0.015,
        outputCostPer1k: 0.075,
      },
    ],
  },
};

const simple = [{ role: 'user', content: 'hi there' }];

// --- parseRouteHeaders ----------------------------------------------------

describe('parseRouteHeaders', () => {
  test('empty headers yield no hints', () => {
    expect(parseRouteHeaders({})).toEqual({ cacheOff: false });
  });

  test('extracts quality, max_cost, route override (case-insensitive)', () => {
    const hints = parseRouteHeaders({
      'X-PS-Quality': 'draft',
      'x-ps-max-cost': '0.02',
      'X-PS-Route': 'frontier',
      'x-ps-cache': 'off',
    });
    expect(hints.quality).toBe('draft');
    expect(hints.maxCost).toBe(0.02);
    expect(hints.explicitGroup).toBe('frontier');
    expect(hints.cacheOff).toBe(true);
  });

  test('ignores an unknown quality value', () => {
    expect(
      parseRouteHeaders({ 'x-ps-quality': 'bogus' }).quality
    ).toBeUndefined();
  });
});

// --- estimation -----------------------------------------------------------

describe('estimation', () => {
  test('estimateInputTokens approximates 4 chars per token', () => {
    const tokens = estimateInputTokens([
      { role: 'user', content: 'a'.repeat(400) },
    ]);
    expect(tokens).toBeGreaterThanOrEqual(90);
    expect(tokens).toBeLessThanOrEqual(110);
  });

  test('estimateTierCost combines input and assumed output cost', () => {
    const tier = policy.groups.balanced[0];
    // 1000 in-tokens * 0.0025/1k + 512 assumed out * 0.01/1k
    const cost = estimateTierCost(tier, 1000, 512);
    expect(cost).toBeCloseTo(0.0025 + 0.00512, 6);
  });
});

// --- HeuristicStrategy ----------------------------------------------------

describe('HeuristicStrategy', () => {
  const strat = new HeuristicStrategy();

  test('routes a short plain prompt to the cheap group', async () => {
    const dec = await strat.route(
      { messages: simple, requestedModel: 'auto', approxInputTokens: 10 },
      policy
    );
    expect(dec.group).toBe('cheap');
    expect(dec.model).toBe('mini');
    expect(dec.reason).toContain('simple');
  });

  test('routes a very long prompt to the frontier group', async () => {
    const dec = await strat.route(
      { messages: simple, requestedModel: 'auto', approxInputTokens: 5000 },
      policy
    );
    expect(dec.group).toBe('frontier');
  });

  test('routes a code-fence prompt to the frontier group', async () => {
    const dec = await strat.route(
      {
        messages: [
          { role: 'user', content: 'fix this:\n```py\nprint(1)\n```' },
        ],
        requestedModel: 'auto',
        approxInputTokens: 20,
      },
      policy
    );
    expect(dec.group).toBe('frontier');
    expect(dec.reason).toContain('complex');
  });

  test('a tight max_cost downgrades a hard prompt below its natural tier', async () => {
    const dec = await strat.route(
      {
        messages: simple,
        requestedModel: 'auto',
        approxInputTokens: 5000, // would be frontier
        maxCost: 0.001, // but frontier busts the budget
      },
      policy
    );
    expect(dec.group).toBe('cheap');
    expect(dec.reason).toContain('budget');
  });
});

// --- resolveRoute (precedence) -------------------------------------------

describe('resolveRoute precedence', () => {
  const strat = new HeuristicStrategy();

  test('explicit group override wins over inferred difficulty', async () => {
    const dec = await resolveRoute(
      {
        messages: simple,
        requestedModel: 'auto',
        approxInputTokens: 10,
        explicitGroup: 'frontier',
      },
      policy,
      strat
    );
    expect(dec.group).toBe('frontier');
    expect(dec.reason).toContain('override');
  });

  test('an unknown explicit group falls back to the default group', async () => {
    const dec = await resolveRoute(
      {
        messages: simple,
        requestedModel: 'auto',
        approxInputTokens: 10,
        explicitGroup: 'nope',
      },
      policy,
      strat
    );
    expect(dec.group).toBe('balanced');
  });

  // Regression: group names are client-controlled (X-PS-Route) and must not
  // resolve to Object.prototype members.
  const PROTO_NAMES = [
    'constructor',
    '__proto__',
    'toString',
    'hasOwnProperty',
  ];

  test.each(PROTO_NAMES)(
    'explicit group %p behaves like any unknown group',
    async (name) => {
      const req = {
        messages: simple,
        requestedModel: 'auto',
        approxInputTokens: 10,
      };
      const unknown = await resolveRoute(
        { ...req, explicitGroup: 'nope' },
        policy,
        strat
      );
      const dec = await resolveRoute(
        { ...req, explicitGroup: name },
        policy,
        strat
      );
      expect(dec).toEqual(unknown);
      expect(dec).toMatchObject({
        model: 'mid',
        group: 'balanced',
        reason: 'override:unknown-group-default',
      });
    }
  );

  test.each(PROTO_NAMES)(
    'X-PS-Route: %s through parseRouteHeaders falls back to the default group',
    async (name) => {
      const hints = parseRouteHeaders({ 'X-PS-Route': name });
      const dec = await resolveRoute(
        {
          messages: simple,
          requestedModel: 'auto',
          approxInputTokens: 10,
          explicitGroup: hints.explicitGroup,
        },
        policy,
        strat
      );
      expect(dec.group).toBe('balanced');
      expect(dec.reason).toBe('override:unknown-group-default');
    }
  );

  test.each(PROTO_NAMES)(
    'quality %p is not mapped through Object.prototype',
    async (name) => {
      expect(parseRouteHeaders({ 'X-PS-Quality': name }).quality).toBe(
        undefined
      );
      // Even if an unvalidated quality reaches resolveRoute directly, it must
      // not pick up a prototype member as its group name.
      const dec = await resolveRoute(
        {
          messages: simple,
          requestedModel: 'auto',
          approxInputTokens: 10,
          quality: name as any,
        },
        policy,
        strat
      );
      expect(dec.group).toBe('balanced');
      expect(dec.model).toBe('mid');
    }
  );

  test('a default group named like a prototype member must be own', async () => {
    const dec = await new HeuristicStrategy().route(
      { messages: simple, requestedModel: 'auto', approxInputTokens: 10 },
      {
        defaultGroup: 'constructor',
        qualityToGroup: policy.qualityToGroup,
        groups: { balanced: policy.groups.balanced },
      }
    );
    expect(dec).toMatchObject({
      model: 'auto',
      group: 'constructor',
      reason: 'heuristic:empty-group-fallback',
      estCost: 0,
    });
  });

  test('quality hint maps to its group regardless of difficulty', async () => {
    const dec = await resolveRoute(
      {
        messages: simple,
        requestedModel: 'auto',
        approxInputTokens: 10,
        quality: 'critical',
      },
      policy,
      strat
    );
    expect(dec.group).toBe('frontier');
    expect(dec.reason).toContain('quality');
  });

  test('no hints falls through to the transparent heuristic', async () => {
    const dec = await resolveRoute(
      { messages: simple, requestedModel: 'auto', approxInputTokens: 10 },
      policy,
      strat
    );
    expect(dec.group).toBe('cheap');
  });

  test('the default policy is a usable, non-empty RoutePolicy', () => {
    expect(Object.keys(defaultPolicy.groups).length).toBeGreaterThan(0);
    expect(defaultPolicy.groups[defaultPolicy.defaultGroup]).toBeDefined();
  });

  test('the default policy is the catalog-generated defaultPolicy.json', () => {
    // Prices come from scripts/build_catalog.py; the SDK test suite fails
    // when this file drifts from the catalog source.
    expect(defaultPolicy.groups).toEqual(generatedPolicy.groups);
    expect(defaultPolicy.qualityToGroup).toEqual(
      generatedPolicy.qualityToGroup
    );
    expect(DEFAULT_POLICY_CATALOG_VERSION).toMatch(/^\d{4}-\d{2}-\d{2}/);
    const mini = defaultPolicy.groups.cheap.find(
      (t) => t.model === 'gpt-4o-mini'
    );
    expect(mini).toMatchObject({
      inputCostPer1k: 0.00015,
      outputCostPer1k: 0.0006,
    });
  });
});
