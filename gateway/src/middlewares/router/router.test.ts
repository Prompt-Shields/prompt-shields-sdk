import {
  defaultPolicy,
  parseRouteHeaders,
  estimateInputTokens,
  estimateTierCost,
  HeuristicStrategy,
  resolveRoute,
  RoutePolicy,
} from './index';

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
});
