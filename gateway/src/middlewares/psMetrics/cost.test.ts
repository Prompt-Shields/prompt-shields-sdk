process.env.PS_API_KEY = 'ps-test';
process.env.PS_TELEMETRY_MAX_ATTEMPTS = '1';

import { Hono } from 'hono';
import {
  MAX_TEAM_LABELS,
  costTotal,
  recordCost,
  resetTeamLabels,
  teamLabel,
} from './cost';
import { psTelemetry } from '../ps-telemetry';

const sample = (labels: string) =>
  costTotal
    .render()
    .split('\n')
    .find((l) => l.startsWith(`ps_gateway_cost_usd_total{${labels}}`));

const value = (labels: string) => Number(sample(labels)?.split(' ').pop() ?? 0);

describe('ps_gateway_cost_usd_total', () => {
  test('counts an event cost by served provider and model, team none', () => {
    const labels = 'provider="mistral",model="mistral-small",team="none"';
    const before = value(labels);
    recordCost({
      vendor: 'openai',
      model: 'auto',
      served_provider: 'mistral',
      served_model: 'mistral-small',
      cost: 0.25,
    });
    recordCost({ vendor: 'mistral', model: 'mistral-small', cost: 0.5 });
    expect(value(labels)).toBeCloseTo(before + 0.75);
  });

  test('ignores missing, zero (cache hit) and invalid costs', () => {
    const labels = 'provider="anthropic",model="claude-x",team="none"';
    recordCost({ vendor: 'anthropic', model: 'claude-x' });
    recordCost({ vendor: 'anthropic', model: 'claude-x', cost: 0 });
    recordCost({ vendor: 'anthropic', model: 'claude-x', cost: NaN });
    recordCost({ vendor: 'anthropic', model: 'claude-x', cost: -1 });
    expect(sample(labels)).toBeUndefined();
  });

  test('a gateway call adds the cost its telemetry event reports', async () => {
    const origFetch = global.fetch;
    global.fetch = jest.fn(async () => ({ ok: true }) as any) as any;
    try {
      const app = new Hono();
      app.use('/v1/chat/completions', psTelemetry());
      app.post('/v1/chat/completions', (c) =>
        c.json({
          model: 'gpt-4o-mini',
          usage: { prompt_tokens: 1000000, completion_tokens: 0 },
        })
      );
      const labels = 'provider="openai",model="gpt-4o-mini",team="none"';
      const before = value(labels);
      await app.request('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-portkey-provider': 'openai',
        },
        body: JSON.stringify({ model: 'gpt-4o-mini', messages: [] }),
      });
      await new Promise((r) => setTimeout(r, 0));
      expect(value(labels)).toBeGreaterThan(before);
    } finally {
      global.fetch = origFetch;
    }
  });

  test('labels the cost with the declared X-PS-Team', async () => {
    const origFetch = global.fetch;
    const sent: any[] = [];
    global.fetch = jest.fn(async (_url: any, init: any) => {
      sent.push(JSON.parse(init.body));
      return { ok: true } as any;
    }) as any;
    try {
      const app = new Hono();
      app.use('/v1/chat/completions', psTelemetry());
      app.post('/v1/chat/completions', (c) =>
        c.json({
          model: 'gpt-4o-mini',
          usage: { prompt_tokens: 1000000, completion_tokens: 0 },
        })
      );
      const labels = 'provider="openai",model="gpt-4o-mini",team="payments"';
      const before = value(labels);
      await app.request('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-portkey-provider': 'openai',
          'x-ps-team': 'payments',
          'x-ps-project': 'checkout',
          'x-ps-service': 'billing-api',
          'x-ps-tags': 'env=prod,cost-center=42',
        },
        body: JSON.stringify({ model: 'gpt-4o-mini', messages: [] }),
      });
      await new Promise((r) => setTimeout(r, 0));
      expect(value(labels)).toBeGreaterThan(before);
      const event = sent.flatMap((b) => b.events ?? [])[0];
      expect(event).toMatchObject({
        team: 'payments',
        project: 'checkout',
        calling_service: 'billing-api',
        tags: 'env=prod,cost-center=42',
      });
    } finally {
      global.fetch = origFetch;
    }
  });

  test('the team label is bounded', () => {
    resetTeamLabels();
    expect(teamLabel(undefined)).toBe('none');
    expect(teamLabel('bad\nvalue')).toBe('other');
    for (let i = 0; i < MAX_TEAM_LABELS; i++) {
      expect(teamLabel(`team-${i}`)).toBe(`team-${i}`);
    }
    expect(teamLabel('one-too-many')).toBe('other');
    expect(teamLabel('team-0')).toBe('team-0');
    resetTeamLabels();
  });
});
