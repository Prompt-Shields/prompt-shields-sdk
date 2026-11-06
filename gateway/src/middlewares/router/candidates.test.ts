/**
 * routeCandidates (#21): the failover list never widens the constraint set.
 * Every candidate after the decided model is one the router could itself
 * have picked: same group, allowed by the data policy and use-case bar, and
 * within maxCost. A seeded property test checks this across random requests.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  fromRoutePolicyDoc,
  HeuristicStrategy,
  resolveRoute,
  routeCandidates,
  RouteRequest,
} from './index';
import {
  classifySensitivity,
  dataPolicyRule,
  tierComplies,
} from './constraints';
import { estimateTierCost } from './policy';

const nordic = fromRoutePolicyDoc(
  JSON.parse(
    fs.readFileSync(
      path.resolve(
        __dirname,
        '../../../../schemas/route-policy/examples/nordic-sovereign.json'
      ),
      'utf8'
    )
  )
);

const req = (over: Partial<RouteRequest> = {}): RouteRequest => ({
  messages: [{ role: 'user', content: 'hi' }],
  requestedModel: 'auto',
  approxInputTokens: 1,
  maxOutputTokens: 512,
  ...over,
});

async function candidates(r: RouteRequest) {
  const decision = await resolveRoute(r, nordic, new HeuristicStrategy());
  return {
    decision,
    models: routeCandidates(r, nordic, decision).map((t) => t.model),
  };
}

describe('routeCandidates', () => {
  test('the decided model first, then compliant tiers of the group by cost', async () => {
    const { decision, models } = await candidates(
      req({ quality: 'draft', dataClassification: 'confidential' })
    );
    expect(decision.model).toBe('scaleway-small');
    expect(models).toEqual(['scaleway-small', 'mistral-small-eu']);
  });

  test('a looser level admits the US model as a fallback', async () => {
    const { models } = await candidates(
      req({ quality: 'draft', dataClassification: 'internal' })
    );
    expect(models).toEqual([
      'scaleway-small',
      'gpt-4o-mini',
      'mistral-small-eu',
    ]);
  });

  test('the use-case eligible list limits fallbacks', async () => {
    const { models } = await candidates(
      req({
        quality: 'draft',
        dataClassification: 'internal',
        useCase: 'claim-summary',
      })
    );
    expect(models).toEqual(['mistral-small-eu']);
  });

  test('maxCost drops fallbacks over the ceiling', async () => {
    const r = req({
      quality: 'draft',
      dataClassification: 'internal',
      maxCost: 0.0003,
    });
    const { models } = await candidates(r);
    for (const m of models.slice(1)) {
      const t = nordic.groups.cheap.find((x) => x.model === m)!;
      expect(estimateTierCost(t, 1, 512)).toBeLessThanOrEqual(0.0003);
    }
    expect(models).not.toContain('mistral-small-eu');
  });

  test('property: every fallback is in the group, compliant and in budget', async () => {
    let seed = 21;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) % 2 ** 31;
      return seed / 2 ** 31;
    };
    const pick = <T>(xs: T[]) => xs[Math.floor(rand() * xs.length)];
    const levels = [
      undefined,
      'public',
      'internal',
      'confidential',
      'restricted',
    ];
    for (let i = 0; i < 300; i++) {
      const r = req({
        quality: pick([undefined, 'draft', 'balanced', 'critical'] as const),
        explicitGroup: pick([undefined, 'cheap', 'frontier', 'nope']),
        dataClassification: pick(levels),
        useCase: pick([undefined, 'legal-review', 'claim-summary']),
        maxCost: pick([undefined, 0.0003, 0.01]),
        approxInputTokens: Math.floor(rand() * 2000),
      });
      let decision;
      try {
        decision = await resolveRoute(r, nordic, new HeuristicStrategy());
      } catch {
        continue; // PolicyViolation: nothing is served
      }
      const list = routeCandidates(r, nordic, decision);
      expect(list[0].model).toBe(decision.model);
      const rule = dataPolicyRule(
        nordic,
        classifySensitivity(nordic, r.dataClassification).level
      );
      const models = list.map((t) => t.model);
      expect(new Set(models).size).toBe(models.length);
      for (const t of list.slice(1)) {
        expect(t.group).toBe(decision.group);
        expect(tierComplies(t, rule)).toBe(true);
        if (r.maxCost != null)
          expect(
            estimateTierCost(t, r.approxInputTokens, 512)
          ).toBeLessThanOrEqual(r.maxCost);
      }
    }
  });
});
