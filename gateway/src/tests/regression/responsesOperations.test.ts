/**
 * Regression tests for the Responses API cancel and input-token-count
 * operations (#73, enterprise 2.25.1), through the whole gateway app and
 * offline: provider routing, clean refusals for providers without them, and
 * the ps-telemetry event each one emits.
 */
jest.mock('jose', () => ({}));

import {
  events,
  json,
  otelSpans,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
  until,
} from './harness';
import app from '../../index';

beforeEach(() => resetHarness());

const OPENAI = {
  'x-portkey-provider': 'openai',
  authorization: 'Bearer sk-regression',
};

const cancelled = {
  id: 'resp_bg1',
  object: 'response',
  status: 'cancelled',
  background: true,
  model: 'gpt-5-2025-08-07',
  output: [],
  usage: { input_tokens: 40, output_tokens: 12, total_tokens: 52 },
};

describe('POST /v1/responses/:id/cancel (#73)', () => {
  test('openai: forwarded to the provider; usage is not counted again', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/resp_bg1\/cancel$/, () =>
      json(cancelled)
    );
    const res = await app.request('/v1/responses/resp_bg1/cancel', {
      method: 'POST',
      headers: OPENAI,
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({
      id: 'resp_bg1',
      status: 'cancelled',
      provider: 'openai',
    });
    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0]).toMatchObject({
      url: 'https://api.openai.com/v1/responses/resp_bg1/cancel',
      method: 'POST',
    });
    expect(upstreamCalls[0].headers.authorization).toBe('Bearer sk-regression');

    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      vendor: 'openai',
      api_surface: 'responses',
      model: 'gpt-5-2025-08-07',
    });
    expect(ev.tokens_in).toBeUndefined();
    expect(ev.tokens_out).toBeUndefined();

    // No GenAI span and no token metrics for the cancel: a create call made
    // after it is the only span exported.
    upstream(/api\.openai\.com\/v1\/responses$/, () =>
      json({ ...cancelled, id: 'resp_2', model: 'gpt-4o-after-cancel' })
    );
    await post(app, '/v1/responses', { model: 'gpt-4o', input: 'x' }, OPENAI);
    await until(() => otelSpans().length > 0, 'otel span');
    expect(otelSpans()).toHaveLength(1);
    const metrics = await (await app.request('/metrics')).text();
    expect(metrics).toMatch(
      /ps_gateway_tokens_total\{[^}]*gpt-4o-after-cancel/
    );
    expect(metrics).not.toMatch(
      /ps_gateway_tokens_total\{[^}]*gpt-5-2025-08-07/
    );
  });

  test('openai: an id containing `/v1` keeps the full upstream path', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/v1abc\/cancel$/, () =>
      json({ ...cancelled, id: 'v1abc' })
    );
    const res = await app.request('/v1/responses/v1abc/cancel', {
      method: 'POST',
      headers: OPENAI,
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://api.openai.com/v1/responses/v1abc/cancel'
    );
  });

  test('openai: an upstream error keeps its status', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/resp_x\/cancel$/, () =>
      json(
        {
          error: {
            message: 'Only background responses can be cancelled.',
            type: 'invalid_request_error',
            param: null,
            code: null,
          },
        },
        400
      )
    );
    const res = await app.request('/v1/responses/resp_x/cancel', {
      method: 'POST',
      headers: OPENAI,
    });
    expect(res.status).toBe(400);
    const out: any = await res.json();
    expect(out.error.message).toContain('background');
  });

  test('azure-openai: deployment resource path with api-version', async () => {
    upstream(
      /regres\.openai\.azure\.com\/openai\/responses\/resp_bg1\/cancel\?/,
      () => json(cancelled)
    );
    const res = await app.request('/v1/responses/resp_bg1/cancel', {
      method: 'POST',
      headers: {
        'x-portkey-provider': 'azure-openai',
        'x-portkey-azure-resource-name': 'regres',
        'x-portkey-azure-deployment-id': 'gpt5',
        'x-portkey-azure-api-version': '2025-04-01-preview',
        authorization: 'Bearer az-key',
      },
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].url).toBe(
      'https://regres.openai.azure.com/openai/responses/resp_bg1/cancel?api-version=2025-04-01-preview'
    );
    expect(upstreamCalls[0].method).toBe('POST');
  });

  test.each(['anthropic', 'groq'])(
    '%s: refused with 400 before any upstream call',
    async (provider) => {
      const res = await app.request('/v1/responses/resp_bg1/cancel', {
        method: 'POST',
        headers: { 'x-portkey-provider': provider, authorization: 'Bearer k' },
      });
      expect(res.status).toBe(400);
      expect(await res.json()).toMatchObject({
        error: {
          type: 'invalid_request_error',
          code: 'unsupported_operation',
        },
      });
      expect(upstreamCalls).toHaveLength(0);
    }
  );
});

describe('POST /v1/responses/input_tokens (#73)', () => {
  const body = {
    model: 'gpt-5',
    instructions: 'Be brief.',
    input: [{ role: 'user', content: 'Hello' }],
    tools: [{ type: 'function', name: 'f', parameters: { type: 'object' } }],
    // Not part of the count request; must not be forwarded.
    stream: true,
    max_output_tokens: 64,
  };

  test('openai: counted by the provider; telemetry carries no token usage', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/input_tokens$/, () =>
      json({ object: 'response.input_tokens', input_tokens: 23 })
    );
    const res = await post(app, '/v1/responses/input_tokens', body, OPENAI);
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({
      object: 'response.input_tokens',
      input_tokens: 23,
    });
    expect(upstreamCalls[0].method).toBe('POST');
    expect(upstreamCalls[0].body).toEqual({
      model: 'gpt-5',
      instructions: 'Be brief.',
      input: [{ role: 'user', content: 'Hello' }],
      tools: body.tools,
    });

    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      vendor: 'openai',
      api_surface: 'responses',
      model: 'gpt-5',
    });
    expect(ev.tokens_in).toBeUndefined();
  });

  test.each(['anthropic', 'azure-openai', 'iointelligence'])(
    '%s: refused with 400 before any upstream call',
    async (provider) => {
      const res = await post(app, '/v1/responses/input_tokens', body, {
        'x-portkey-provider': provider,
        authorization: 'Bearer k',
      });
      expect(res.status).toBe(400);
      const out: any = await res.json();
      expect(out.error.message).toContain('/v1/responses/input_tokens');
      expect(upstreamCalls).toHaveLength(0);
    }
  );

  test('the existing GET /v1/responses/:id route is unaffected', async () => {
    upstream(/api\.openai\.com\/v1\/responses\/resp_1$/, () =>
      json({ ...cancelled, id: 'resp_1', status: 'completed' })
    );
    const res = await app.request('/v1/responses/resp_1', {
      method: 'GET',
      headers: OPENAI,
    });
    expect(res.status).toBe(200);
    expect(upstreamCalls[0].method).toBe('GET');
    // GETs emit no telemetry.
    expect(events).toHaveLength(0);
  });
});
