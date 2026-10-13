/**
 * OTel GenAI spans (#70): semconv 1.40 attributes, content off by default,
 * batched fail-open OTLP/HTTP JSON export.
 */
import { Hono } from 'hono';
import {
  buildExportRequest,
  buildGenAISpan,
  contentAttributes,
  finishReasonsOf,
  OtelConfig,
  otelConfig,
  OtlpJsonExporter,
  parseHeaderList,
  psOtel,
  semconvProviderName,
} from './psOtel';

const attrsOf = (span: any) =>
  Object.fromEntries(
    span.attributes.map((a: any) => [
      a.key,
      a.value.stringValue ??
        (a.value.intValue !== undefined
          ? Number(a.value.intValue)
          : undefined) ??
        a.value.doubleValue ??
        a.value.boolValue ??
        a.value.arrayValue?.values.map((v: any) => v.stringValue),
    ])
  );

const config = (over: Partial<OtelConfig> = {}): OtelConfig => ({
  endpoint: 'http://otel.test/v1/traces',
  headers: {},
  serviceName: 'gw',
  captureContent: false,
  maxBatch: 2,
  flushMs: 60_000,
  maxQueue: 10,
  ...over,
});

const chatReq = {
  model: 'gpt-4o-mini',
  temperature: 0.2,
  max_tokens: 100,
  messages: [
    { role: 'system', content: 'be terse' },
    { role: 'user', content: 'secret prompt' },
  ],
};
const chatRes = {
  id: 'chatcmpl-1',
  model: 'gpt-4o-mini-2024-07-18',
  choices: [
    {
      message: { role: 'assistant', content: 'secret answer' },
      finish_reason: 'stop',
    },
  ],
  usage: {
    prompt_tokens: 11,
    completion_tokens: 22,
    prompt_tokens_details: { cached_tokens: 4 },
  },
};

describe('otelConfig', () => {
  test('off unless an endpoint is set', () => {
    expect(otelConfig({})).toBeNull();
  });

  test('reads endpoint, headers, content flag; adds PS key as bearer', () => {
    const c = otelConfig({
      PS_OTEL_ENDPOINT: 'http://c/ingest/otel/v1/traces',
      PS_API_KEY: 'k',
      OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT: 'true',
    })!;
    expect(c.endpoint).toBe('http://c/ingest/otel/v1/traces');
    expect(c.headers.Authorization).toBe('Bearer k');
    expect(c.captureContent).toBe(true);
    expect(otelConfig({ PS_OTEL_ENDPOINT: 'x' })!.captureContent).toBe(false);
  });

  test('never sends PS key to a generic OTEL_* endpoint', () => {
    const c = otelConfig({
      OTEL_EXPORTER_OTLP_TRACES_ENDPOINT:
        'https://otel.vendor.example/v1/traces',
      OTEL_EXPORTER_OTLP_TRACES_HEADERS: 'x-vendor-key=v',
      PS_API_KEY: 'k',
    })!;
    expect(c.headers).toEqual({ 'x-vendor-key': 'v' });
  });

  test('malformed header escape does not throw', () => {
    expect(parseHeaderList('a=%zz,b=1')).toEqual({ a: '%zz', b: '1' });
  });

  test('explicit authorization header wins over PS key', () => {
    const c = otelConfig({
      OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: 'x',
      OTEL_EXPORTER_OTLP_TRACES_HEADERS: 'authorization=Basic%20abc,x-a=1',
      PS_API_KEY: 'k',
    })!;
    expect(c.headers).toEqual({ authorization: 'Basic abc', 'x-a': '1' });
  });

  test('parseHeaderList ignores junk', () => {
    expect(parseHeaderList('=a,b,c=d')).toEqual({ c: 'd' });
  });
});

describe('buildGenAISpan', () => {
  const base = {
    path: '/v1/chat/completions',
    headers: {
      'x-portkey-provider': 'bedrock',
      'x-ps-business-unit': 'payments',
      'x-ps-use-case': 'support-bot',
      'x-ps-environment': 'prod',
    },
    status: 200,
    startMs: 1_700_000_000_000,
    endMs: 1_700_000_000_250,
    requestBody: chatReq,
    responseBody: chatRes,
  };

  test('semconv 1.40 attributes for a chat call', () => {
    const { span, environment } = buildGenAISpan(base)!;
    const a = attrsOf(span);
    expect(span.name).toBe('chat gpt-4o-mini');
    expect(span.kind).toBe(3);
    expect(span.traceId).toMatch(/^[0-9a-f]{32}$/);
    expect(span.spanId).toMatch(/^[0-9a-f]{16}$/);
    expect(span.endTimeUnixNano).toBe('1700000000250000000');
    expect(span.status).toEqual({ code: 0 });
    expect(a).toMatchObject({
      'gen_ai.operation.name': 'chat',
      'gen_ai.provider.name': 'aws.bedrock',
      'gen_ai.request.model': 'gpt-4o-mini',
      'gen_ai.request.temperature': 0.2,
      'gen_ai.request.max_tokens': 100,
      'gen_ai.response.model': 'gpt-4o-mini-2024-07-18',
      'gen_ai.response.id': 'chatcmpl-1',
      'gen_ai.response.finish_reasons': ['stop'],
      'gen_ai.usage.input_tokens': 11,
      'gen_ai.usage.output_tokens': 22,
      'gen_ai.usage.cache_read.input_tokens': 4,
      'ps.business_unit': 'payments',
      'ps.use_case': 'support-bot',
    });
    expect(a['error.type']).toBeUndefined();
    expect(environment).toBe('prod');
  });

  test('content is never attached by default', () => {
    const json = JSON.stringify(buildGenAISpan(base));
    expect(json).not.toContain('secret');
    expect(json).not.toContain('be terse');
    expect(json).not.toContain('gen_ai.input.messages');
  });

  test('content attached as structured messages when opted in', () => {
    const a = attrsOf(buildGenAISpan({ ...base, captureContent: true })!.span);
    expect(JSON.parse(a['gen_ai.system_instructions'])).toEqual([
      { type: 'text', content: 'be terse' },
    ]);
    expect(JSON.parse(a['gen_ai.input.messages'])).toEqual([
      { role: 'user', parts: [{ type: 'text', content: 'secret prompt' }] },
    ]);
    expect(JSON.parse(a['gen_ai.output.messages'])).toEqual([
      {
        role: 'assistant',
        parts: [{ type: 'text', content: 'secret answer' }],
        finish_reason: 'stop',
      },
    ]);
  });

  test('errors set error.type and an ERROR status', () => {
    const { span } = buildGenAISpan({
      ...base,
      status: 429,
      responseBody: {
        error: { type: 'rate_limit_error', message: 'slow down' },
      },
    })!;
    expect(attrsOf(span)['error.type']).toBe('rate_limit_error');
    expect(span.status.code).toBe(2);
    const plain = buildGenAISpan({
      ...base,
      status: 502,
      responseBody: undefined,
    })!;
    expect(attrsOf(plain.span)['error.type']).toBe('502');
  });

  test('Anthropic Messages: tool_use normalised, cache tokens counted', () => {
    const a = attrsOf(
      buildGenAISpan({
        ...base,
        path: '/v1/messages',
        headers: { 'x-portkey-provider': 'anthropic' },
        requestBody: { model: 'claude-x', messages: [] },
        responseBody: {
          type: 'message',
          model: 'claude-x',
          stop_reason: 'tool_use',
          usage: {
            input_tokens: 5,
            cache_read_input_tokens: 10,
            output_tokens: 3,
          },
        },
      })!.span
    );
    expect(a['gen_ai.operation.name']).toBe('chat');
    expect(a['ps.api_surface']).toBe('messages');
    expect(a['gen_ai.provider.name']).toBe('anthropic');
    expect(a['gen_ai.response.finish_reasons']).toEqual(['tool_call']);
    expect(a['gen_ai.usage.input_tokens']).toBe(15);
    expect(a['gen_ai.usage.cache_read.input_tokens']).toBe(10);
  });

  test('streams use the stream summary and mark truncation as an error', () => {
    const { span } = buildGenAISpan({
      ...base,
      responseBody: undefined,
      captureContent: true,
      stream: {
        tokens_in: 7,
        tokens_out: 9,
        model: 'm-served',
        terminal: false,
        truncated: true,
        cancelled: false,
      },
    })!;
    const a = attrsOf(span);
    expect(a['gen_ai.usage.input_tokens']).toBe(7);
    expect(a['gen_ai.response.model']).toBe('m-served');
    expect(a['error.type']).toBe('stream_truncated');
    expect(a['gen_ai.input.messages']).toBeUndefined();
  });

  test('joins the caller trace from traceparent', () => {
    const { span } = buildGenAISpan({
      ...base,
      headers: {
        traceparent: '00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01',
      },
    })!;
    expect(span.traceId).toBe('0af7651916cd43dd8448eb211c80319c');
    expect(span.parentSpanId).toBe('b7ad6b7169203331');
  });

  test('non-numeric request params are dropped', () => {
    const a = attrsOf(
      buildGenAISpan({
        ...base,
        requestBody: { model: 'm', temperature: 'hot' },
      })!.span
    );
    expect(a['gen_ai.request.temperature']).toBeUndefined();
  });

  test('non-LLM paths produce no span', () => {
    expect(buildGenAISpan({ ...base, path: '/v1/files' })).toBeUndefined();
  });
});

describe('helpers', () => {
  test('provider ids map to semconv well-known names', () => {
    expect(semconvProviderName('vertex-ai')).toBe('gcp.vertex_ai');
    expect(semconvProviderName('azure-openai')).toBe('azure.ai.openai');
    expect(semconvProviderName('together-ai')).toBe('together-ai');
    expect(semconvProviderName(undefined)).toBe('unknown');
  });

  test('Responses API finish reasons', () => {
    expect(
      finishReasonsOf({ object: 'response', status: 'completed' })
    ).toEqual(['stop']);
    expect(
      finishReasonsOf({
        object: 'response',
        status: 'incomplete',
        incomplete_details: { reason: 'max_output_tokens' },
      })
    ).toEqual(['length']);
  });

  test('chat tool calls become tool_call parts', () => {
    const out = contentAttributes(
      {
        messages: [
          {
            role: 'assistant',
            content: null,
            tool_calls: [
              { id: 't1', function: { name: 'f', arguments: '{"a":1}' } },
            ],
          },
          { role: 'tool', tool_call_id: 't1', content: 'ok' },
        ],
      },
      undefined
    );
    expect(JSON.parse(out['gen_ai.input.messages'])).toEqual([
      {
        role: 'assistant',
        parts: [
          { type: 'tool_call', id: 't1', name: 'f', arguments: { a: 1 } },
        ],
      },
      {
        role: 'tool',
        parts: [{ type: 'tool_call_response', id: 't1', response: 'ok' }],
      },
    ]);
  });

  test('export request groups spans by environment on the resource', () => {
    const body = buildExportRequest(
      [
        { span: { spanId: 'a' }, environment: 'prod' },
        { span: { spanId: 'b' } },
        { span: { spanId: 'c' }, environment: 'prod' },
      ],
      'gw'
    );
    expect(body.resourceSpans).toHaveLength(2);
    expect(attrsOf(body.resourceSpans[0].resource)).toEqual({
      'service.name': 'gw',
      'deployment.environment.name': 'prod',
    });
    expect(
      body.resourceSpans[0].scopeSpans[0].spans.map((s: any) => s.spanId)
    ).toEqual(['a', 'c']);
  });
});

describe('OtlpJsonExporter', () => {
  const span = (id: string) => ({ span: { spanId: id } });

  test('batches by size and sends OTLP JSON with headers', async () => {
    const fetchFn = jest.fn(async () => ({ ok: true }) as any);
    const ex = new OtlpJsonExporter(
      config({ headers: { Authorization: 'Bearer k' } }),
      fetchFn
    );
    ex.enqueue(span('a'));
    expect(fetchFn).not.toHaveBeenCalled();
    ex.enqueue(span('b'));
    await ex.flush();
    expect(fetchFn).toHaveBeenCalledTimes(1);
    const [url, init] = fetchFn.mock.calls[0] as any[];
    expect(url).toBe('http://otel.test/v1/traces');
    expect(init.headers).toMatchObject({
      'Content-Type': 'application/json',
      Authorization: 'Bearer k',
    });
    const body = JSON.parse(init.body);
    expect(body.resourceSpans[0].scopeSpans[0].spans).toHaveLength(2);
    expect(body.resourceSpans[0].scopeSpans[0].schemaUrl).toContain('1.40.0');
  });

  test('flushes on a timer when the batch is not full', async () => {
    jest.useFakeTimers();
    try {
      const fetchFn = jest.fn(async () => ({ ok: true }) as any);
      const ex = new OtlpJsonExporter(config({ flushMs: 1000 }), fetchFn);
      ex.enqueue(span('a'));
      jest.advanceTimersByTime(1000);
      expect(fetchFn).toHaveBeenCalledTimes(1);
      expect(ex.pending).toBe(0);
    } finally {
      jest.useRealTimers();
    }
  });

  test('flushes immediately under waitUntil (Workers)', async () => {
    const fetchFn = jest.fn(async () => ({ ok: true }) as any);
    const ex = new OtlpJsonExporter(config({ maxBatch: 50 }), fetchFn);
    const kept: Promise<unknown>[] = [];
    ex.enqueue(span('a'), (p) => kept.push(p));
    await Promise.all(kept);
    expect(kept).toHaveLength(1);
    expect(fetchFn).toHaveBeenCalledTimes(1);
  });

  test('fail-open: export errors are swallowed; queue is bounded', async () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
    const fetchFn = jest.fn(async () => {
      throw new Error('down');
    });
    const ex = new OtlpJsonExporter(
      config({ maxBatch: 100, maxQueue: 3 }),
      fetchFn
    );
    for (const id of ['a', 'b', 'c', 'd', 'e']) ex.enqueue(span(id));
    expect(ex.pending).toBe(3);
    await expect(ex.flush()).resolves.toBeUndefined();
    await ex.flush();
    expect(warn).toHaveBeenCalledTimes(1);
    warn.mockRestore();
  });
});

describe('psOtel (Hono middleware)', () => {
  const makeApp = (handler: (c: any) => any, captureContent = false) => {
    const sent: any[] = [];
    const exporter = { enqueue: (s: any) => sent.push(s) } as any;
    const app = new Hono();
    app.use('*', psOtel(config({ captureContent }), exporter));
    app.post('/v1/chat/completions', handler);
    app.post('/v1/files', (c) => c.json({}));
    return { app, sent };
  };
  const post = (app: Hono, path: string, body: any) =>
    app.request(path, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-portkey-provider': 'openai',
      },
      body: JSON.stringify(body),
    });

  test('emits one span per LLM call without content', async () => {
    const { app, sent } = makeApp((c) => c.json(chatRes));
    const res = await post(app, '/v1/chat/completions', chatReq);
    expect(res.status).toBe(200);
    expect(sent).toHaveLength(1);
    const a = attrsOf(sent[0].span);
    expect(a['gen_ai.provider.name']).toBe('openai');
    expect(a['gen_ai.usage.output_tokens']).toBe(22);
    expect(JSON.stringify(sent)).not.toContain('secret');
  });

  test('skips non-LLM paths', async () => {
    const { app, sent } = makeApp((c) => c.json(chatRes));
    await post(app, '/v1/files', {});
    expect(sent).toHaveLength(0);
  });

  test('captures content only when configured', async () => {
    const { app, sent } = makeApp((c) => c.json(chatRes), true);
    await post(app, '/v1/chat/completions', chatReq);
    expect(attrsOf(sent[0].span)['gen_ai.output.messages']).toContain(
      'secret answer'
    );
  });
});
