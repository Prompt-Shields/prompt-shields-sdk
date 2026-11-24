/**
 * include_usage injection and stripping (#389), unit level; the end-to-end
 * behaviour is in tests/regression/streamUsage.test.ts.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  DEFAULT_PROVIDERS,
  injectIncludeUsage,
  streamUsageEnabled,
  stripEvent,
  stripInjectedUsage,
} from './psStreamUsage';

describe('streamUsageEnabled', () => {
  test('default allowlist', () => {
    expect(streamUsageEnabled({}, 'openai')).toBe(true);
    expect(
      streamUsageEnabled({}, 'azure-openai', undefined, '2024-10-21')
    ).toBe(true);
    expect(streamUsageEnabled({}, 'azure-openai', undefined, 'v1')).toBe(true);
    expect(
      streamUsageEnabled({}, 'azure-openai', undefined, '2024-02-01')
    ).toBe(false);
    expect(streamUsageEnabled({}, 'azure-openai')).toBe(false);
    expect(streamUsageEnabled({}, 'groq')).toBe(false);
  });

  test('turned off, replaced, and custom hosts', () => {
    expect(
      streamUsageEnabled({ PS_STREAM_INCLUDE_USAGE: 'false' }, 'openai')
    ).toBe(false);
    expect(
      streamUsageEnabled(
        { PS_STREAM_INCLUDE_USAGE_PROVIDERS: 'groq, x-ai' },
        'x-ai'
      )
    ).toBe(true);
    expect(
      streamUsageEnabled(
        { PS_STREAM_INCLUDE_USAGE_PROVIDERS: 'groq' },
        'openai'
      )
    ).toBe(false);
    expect(streamUsageEnabled({}, 'openai', 'https://vllm.local/v1')).toBe(
      false
    );
    expect(
      streamUsageEnabled(
        { PS_STREAM_INCLUDE_USAGE_HOSTS: 'vllm.local' },
        'openai',
        'https://vllm.local/v1'
      )
    ).toBe(true);
  });

  test('matches the shared allowlist', () => {
    const doc = JSON.parse(
      fs.readFileSync(
        path.join(
          __dirname,
          '../../../schemas/stream-usage/include-usage.v1.json'
        ),
        'utf8'
      )
    );
    expect([...DEFAULT_PROVIDERS].sort()).toEqual(
      [...doc.gateway_providers].sort()
    );
  });
});

describe('injectIncludeUsage', () => {
  test.each([
    [{ stream: true }, true, { include_usage: true }],
    [{ stream: true, stream_options: null }, true, { include_usage: true }],
    [
      { stream: true, stream_options: { x: 1 } },
      true,
      { x: 1, include_usage: true },
    ],
    [
      { stream: true, stream_options: { include_usage: false } },
      false,
      { include_usage: false },
    ],
    [
      { stream: true, stream_options: { include_usage: true } },
      false,
      { include_usage: true },
    ],
    [{ stream: false }, false, undefined],
    [{}, false, undefined],
  ])('%j', (body: any, injected, opts) => {
    expect(injectIncludeUsage(body)).toBe(injected);
    expect(body.stream_options ?? undefined).toEqual(opts);
  });
});

describe('stripEvent', () => {
  const seen: string[] = [];
  const onUsage = (d: string) => seen.push(d);
  beforeEach(() => (seen.length = 0));

  test('usage-only chunk is removed and handed over', () => {
    const data = '{"id":"1","choices":[],"usage":{"prompt_tokens":3}}';
    expect(stripEvent(`data: ${data}\n\n`, onUsage)).toBeUndefined();
    expect(seen).toEqual([data]);
  });

  test('"usage":null is removed, in any position, for compact JSON', () => {
    expect(
      stripEvent('data: {"id":"1","choices":[],"usage":null}\n\n', onUsage)
    ).toBe('data: {"id":"1","choices":[]}\n\n');
    expect(
      stripEvent('data: {"usage":null,"id":"1","choices":[]}\r\n\r\n', onUsage)
    ).toBe('data: {"id":"1","choices":[]}\r\n\r\n');
  });

  test('usage on a chunk with choices is removed and handed over', () => {
    const data =
      '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3}}';
    expect(stripEvent(`data: ${data}\n\n`, onUsage)).toBe(
      'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    );
    expect(seen).toEqual([data]);
  });

  test.each([
    'data: [DONE]\n\n',
    ': keep-alive\n\n',
    'data: {"choices":[{"delta":{"content":"usage"}}]}\n\n',
    'data: { "choices": [], "usage": null }\n\n',
    'data: not json "usage"\n\n',
  ])('left unchanged: %j', (event) => {
    expect(stripEvent(event, onUsage)).toBe(event);
  });
});

describe('stripInjectedUsage', () => {
  const read = async (res: Response) => await res.text();

  test('non-SSE and error responses pass through', () => {
    const json = new Response('{}', {
      headers: { 'content-type': 'application/json' },
    });
    expect(stripInjectedUsage(json, { lines: [] })).toBe(json);
    const err = new Response('x', {
      status: 500,
      headers: { 'content-type': 'text/event-stream' },
    });
    expect(stripInjectedUsage(err, { lines: [] })).toBe(err);
  });

  test('an unterminated last event is flushed as is', async () => {
    const res = new Response('data: {"a":1,"usage":null}\n\ndata: {"b"', {
      headers: { 'content-type': 'text/event-stream' },
    });
    const sideband = { lines: [] as string[] };
    expect(await read(stripInjectedUsage(res, sideband))).toBe(
      'data: {"a":1}\n\ndata: {"b"'
    );
  });
});
