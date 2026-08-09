/**
 * Verifies that a routing decision stashed on the request (_psRoute) surfaces
 * in the gateway telemetry event as requested_model / served_model / route_*.
 */
process.env.PS_API_KEY = 'ps-test';

import { psTelemetryMiddleware } from './ps-telemetry';

describe('ps-telemetry routing fields', () => {
  const origFetch = global.fetch;
  let sent: any;

  beforeEach(() => {
    sent = undefined;
    global.fetch = jest.fn(async (_url: any, init: any) => {
      sent = JSON.parse(init.body);
      return { ok: true } as any;
    }) as any;
  });

  afterAll(() => {
    global.fetch = origFetch;
  });

  const flush = () => new Promise((r) => setTimeout(r, 0));

  test('emits requested_model, served_model and route metadata', async () => {
    const mw = psTelemetryMiddleware();
    const request: any = {
      url: 'https://api.openai.com/v1/chat/completions',
      headers: {},
      body: { model: 'gpt-4o-mini' },
      _psStartTime: Date.now(),
      _psRoute: {
        requestedModel: 'auto',
        servedModel: 'gpt-4o-mini',
        group: 'cheap',
        reason: 'heuristic:simple',
        estCost: 0.0004,
      },
    };
    mw.afterResponse(request, {
      body: { model: 'gpt-4o-mini', usage: { prompt_tokens: 5, completion_tokens: 7 } },
      status: 200,
    });
    await flush();

    expect(sent.events[0].requested_model).toBe('auto');
    expect(sent.events[0].served_model).toBe('gpt-4o-mini');
    expect(sent.events[0].route_group).toBe('cheap');
    expect(sent.events[0].route_reason).toBe('heuristic:simple');
  });

  test('omits routing fields when no decision was made', async () => {
    const mw = psTelemetryMiddleware();
    const request: any = {
      url: 'https://api.openai.com/v1/chat/completions',
      headers: {},
      body: { model: 'gpt-4o' },
      _psStartTime: Date.now(),
    };
    mw.afterResponse(request, { body: { model: 'gpt-4o' }, status: 200 });
    await flush();

    expect(sent.events[0].requested_model).toBeUndefined();
    expect(sent.events[0].route_group).toBeUndefined();
  });
});
