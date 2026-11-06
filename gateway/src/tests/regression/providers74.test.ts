/**
 * Offline regression tests for the #74 fifth slice:
 * - the Meta Llama API (`meta`) on the Messages (`/v1/messages`) and
 *   Responses (`/v1/responses`) routes, through the gateway's adapters onto
 *   Meta's OpenAI-compatible chat endpoint;
 * - Amazon Bedrock cross-region inference profiles in model resolution,
 *   including the India (`in.`) and GovCloud (`us-gov.`) prefixes, for
 *   routing (the base model picks the transform) and for telemetry pricing
 *   (a profile is priced as its base model).
 */
jest.mock('jose', () => ({}));

import {
  json,
  post,
  resetHarness,
  telemetryEvent,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import {
  getBedrockModelWithoutRegion,
  getBedrockFoundationModel,
} from '../../providers/bedrock/utils';
import { getProviderAndModel } from '../../providers/bedrock';
import { costFields, withBedrockBaseModels } from '../../middlewares/psPricing';

beforeEach(() => resetHarness());

describe('Meta Llama API: Messages and Responses routes', () => {
  const MODEL = 'Llama-4-Maverick-17B-128E-Instruct-FP8';
  const headers = {
    'x-portkey-provider': 'meta',
    authorization: 'Bearer llama-regression',
  };
  const completion = {
    id: 'chatcmpl-1',
    object: 'chat.completion',
    created: 1,
    model: MODEL,
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'Hi' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 9, completion_tokens: 1, total_tokens: 10 },
  };

  test('/v1/messages is translated to chat completions', async () => {
    upstream(/api\.llama\.com\/compat\/v1\/chat\/completions$/, () =>
      json(completion)
    );
    const res = await post(
      app,
      '/v1/messages',
      {
        model: MODEL,
        max_tokens: 16,
        system: 'Be brief.',
        messages: [{ role: 'user', content: 'Say hi' }],
      },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.type).toBe('message');
    expect(body.role).toBe('assistant');
    expect(body.content).toEqual([{ type: 'text', text: 'Hi' }]);
    expect(body.stop_reason).toBe('end_turn');
    expect(body.usage).toMatchObject({ input_tokens: 9, output_tokens: 1 });

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(
      'https://api.llama.com/compat/v1/chat/completions'
    );
    expect(upstreamCalls[0].headers.authorization).toBe(
      'Bearer llama-regression'
    );
    expect(upstreamCalls[0].body).toMatchObject({
      model: MODEL,
      // The Messages adapter sends OpenAI's current name for the limit.
      max_completion_tokens: 16,
      messages: [
        { role: 'system', content: 'Be brief.' },
        { role: 'user', content: 'Say hi' },
      ],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta',
      model: MODEL,
      tokens_in: 9,
      tokens_out: 1,
    });
  });

  test('/v1/responses is translated to chat completions', async () => {
    upstream(/api\.llama\.com\/compat\/v1\/chat\/completions$/, () =>
      json(completion)
    );
    const res = await post(
      app,
      '/v1/responses',
      { model: MODEL, input: 'Say hi', max_output_tokens: 16 },
      headers
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.object).toBe('response');
    expect(body.status).toBe('completed');
    const text = body.output
      .filter((o: any) => o.type === 'message')
      .flatMap((o: any) => o.content)
      .map((c: any) => c.text)
      .join('');
    expect(text).toBe('Hi');
    expect(body.usage).toMatchObject({ input_tokens: 9, output_tokens: 1 });

    expect(upstreamCalls).toHaveLength(1);
    expect(upstreamCalls[0].url).toBe(
      'https://api.llama.com/compat/v1/chat/completions'
    );
    expect(upstreamCalls[0].body).toMatchObject({
      model: MODEL,
      messages: [{ role: 'user', content: 'Say hi' }],
    });

    expect(await telemetryEvent()).toMatchObject({
      vendor: 'meta',
      model: MODEL,
      tokens_in: 9,
      tokens_out: 1,
    });
  });
});

describe('Bedrock cross-region inference profiles', () => {
  const BASE = 'anthropic.claude-3-5-haiku-20241022-v1:0';

  test.each(['us', 'us-gov', 'eu', 'apac', 'au', 'ca', 'jp', 'in', 'global'])(
    '%s. profile resolves to the base model',
    (prefix) => {
      const model = `${prefix}.${BASE}`;
      expect(getBedrockModelWithoutRegion(model)).toBe(BASE);
      expect(getProviderAndModel({ model })).toEqual({
        provider: 'anthropic',
        model: 'claude-3-5-haiku-20241022-v1:0',
      });
    }
  );

  test('model ids without a profile prefix are unchanged', () => {
    expect(getBedrockModelWithoutRegion(BASE)).toBe(BASE);
    expect(getBedrockModelWithoutRegion('amazon.nova-pro-v1:0')).toBe(
      'amazon.nova-pro-v1:0'
    );
  });

  test('a foundation-model ARN resolves without any AWS call', async () => {
    expect(
      await getBedrockFoundationModel(
        `arn:aws:bedrock:us-east-1::foundation-model/${BASE}`,
        {} as any,
        {}
      )
    ).toBe(BASE);
  });

  test('pricing tries the base model after the profile id', () => {
    expect(withBedrockBaseModels('bedrock', [`in.${BASE}`])).toEqual([
      `in.${BASE}`,
      BASE,
    ]);
    expect(withBedrockBaseModels('bedrock', [BASE])).toEqual([BASE]);
    // Other vendors are never rewritten.
    expect(withBedrockBaseModels('openai', ['us.gpt-4o'])).toEqual([
      'us.gpt-4o',
    ]);
    const usage = { tokens_in: 1000, tokens_out: 100 };
    const base = costFields('bedrock', BASE, undefined, usage);
    expect(base.cost).toBeGreaterThan(0);
    expect(costFields('bedrock', `in.${BASE}`, undefined, usage)).toEqual(base);
    expect(costFields('bedrock', `us-gov.${BASE}`, undefined, usage)).toEqual(
      base
    );
  });

  test('an in. profile call is routed by its base model and priced', async () => {
    const model = `in.${BASE}`;
    upstream(/\/converse$/, () =>
      json({
        output: { message: { role: 'assistant', content: [{ text: 'Hi' }] } },
        stopReason: 'end_turn',
        usage: { inputTokens: 1000, outputTokens: 100, totalTokens: 1100 },
      })
    );
    const res = await post(
      app,
      '/v1/chat/completions',
      {
        model,
        messages: [{ role: 'user', content: 'Say hi' }],
        max_tokens: 16,
      },
      {
        'x-portkey-config': JSON.stringify({
          provider: 'bedrock',
          aws_access_key_id: 'AKIAREGRESSIONTEST',
          aws_secret_access_key: 'regression-secret',
          aws_region: 'ap-south-1',
        }),
      }
    );
    expect(res.status).toBe(200);
    const body: any = await res.json();
    expect(body.choices[0].message.content).toBe('Hi');
    // The profile id is what Bedrock is called with.
    expect(upstreamCalls[0].url).toBe(
      `https://bedrock-runtime.ap-south-1.amazonaws.com/model/${encodeURIComponent(model)}/converse`
    );
    // Anthropic-on-Bedrock transform: max_tokens goes to inferenceConfig.
    expect(upstreamCalls[0].body.inferenceConfig).toMatchObject({
      maxTokens: 16,
    });
    const ev = await telemetryEvent();
    expect(ev).toMatchObject({
      vendor: 'bedrock',
      model,
      tokens_in: 1000,
      tokens_out: 100,
      cost_source: 'pricing_table',
    });
    expect(ev.cost).toEqual(
      costFields('bedrock', BASE, undefined, {
        tokens_in: 1000,
        tokens_out: 100,
      }).cost
    );
  });
});
