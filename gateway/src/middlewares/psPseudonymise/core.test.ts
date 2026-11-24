/**
 * Pseudonymisation core (#350): privacy guards, Responses shapes, stream
 * events, tool-argument JSON and linear-time behaviour on hostile input.
 */
import { inspect } from 'util';
import { findEntitySpans } from '../router/pii';
import {
  parsePseudonymisationSection,
  pseudonymisationFromEnv,
} from './config';
import { CLOSE, OPEN, Pseudonymisation } from './core';
import { PyJsonError, pyDumps, pyLoads } from './pyjson';
import { ChunkRestorer, restoreSSE } from './stream';

const EMAIL = 'kari@example.no';
const tok = (s: string) => `${OPEN}${s}${CLOSE}`;

describe('Pseudonymisation', () => {
  test('the mapping is never printed or serialised', () => {
    const p = new Pseudonymisation();
    p.pseudonymiseChat([{ role: 'user', content: `mail ${EMAIL}` }]);
    expect(p.active).toBe(true);
    for (const shown of [
      String(p),
      JSON.stringify(p),
      inspect(p),
      JSON.stringify(p.stats()),
    ])
      expect(shown).not.toContain(EMAIL);
    p.clear();
    expect(p.active).toBe(false);
    expect(p.restore(tok('EMAIL_1'))).toBe(tok('EMAIL_1'));
  });

  test('Responses requests: instructions first, then input items', () => {
    const p = new Pseudonymisation();
    const body = {
      model: 'm',
      instructions: `Owner ${EMAIL}`,
      input: [
        {
          role: 'user',
          content: [{ type: 'input_text', text: 'me: a@example.com' }],
        },
        {
          type: 'function_call',
          call_id: 'c',
          name: 'f',
          arguments: '{"to":"a@example.com"}',
        },
        {
          type: 'function_call_output',
          call_id: 'c',
          output: `sent to ${EMAIL}`,
        },
        {
          type: 'custom_tool_call',
          call_id: 'd',
          name: 'g',
          input: '555-123-4567',
        },
        { type: 'input_image', image_url: 'https://x/a@example.com' },
      ],
      prompt: { id: 'p', variables: { who: 'b@example.com', n: 3 } },
    };
    const out = p.pseudonymiseBody('responses', body);
    expect(out.instructions).toBe(`Owner ${tok('EMAIL_1')}`);
    expect(out.input[0].content[0].text).toBe(`me: ${tok('EMAIL_2')}`);
    expect(out.input[1].arguments).toBe(`{"to": "${tok('EMAIL_2')}"}`);
    expect(out.input[2].output).toBe(`sent to ${tok('EMAIL_1')}`);
    expect(out.input[3].input).toBe(tok('PHONE_1'));
    expect(out.input[4]).toEqual(body.input[4]);
    expect(out.prompt.variables).toEqual({ who: tok('EMAIL_3'), n: 3 });
    // Everything but the image URL (not text, kept as it is).
    expect(
      JSON.stringify({ ...out, input: out.input.slice(0, 4) })
    ).not.toMatch(/example\.(no|com)|555-123/);
    expect(body.instructions).toBe(`Owner ${EMAIL}`); // input not changed
  });

  test('a string Responses input', () => {
    const p = new Pseudonymisation();
    expect(p.pseudonymiseBody('responses', { input: EMAIL }).input).toBe(
      tok('EMAIL_1')
    );
  });

  test('restores whole chat, Messages and Responses answers', () => {
    const p = new Pseudonymisation();
    p.pseudonymiseChat([{ role: 'user', content: EMAIL }]);
    const e = tok('EMAIL_1');
    const chat = p.restoreResponse({
      choices: [
        {
          message: {
            content: `hi ${e}`,
            tool_calls: [{ function: { arguments: `{"to":"${e}"}` } }],
          },
        },
      ],
    });
    expect(chat.choices[0].message.content).toBe(`hi ${EMAIL}`);
    expect(chat.choices[0].message.tool_calls[0].function.arguments).toBe(
      `{"to":"${EMAIL}"}`
    );
    const msg = p.restoreResponse({
      content: [
        { type: 'text', text: e },
        { type: 'tool_use', input: { [e]: [e] } },
        { type: 'thinking', thinking: e },
      ],
    });
    expect(msg.content[0].text).toBe(EMAIL);
    expect(msg.content[1].input).toEqual({ [EMAIL]: [EMAIL] });
    expect(msg.content[2].thinking).toBe(e);
    const resp = p.restoreResponse({
      object: 'response',
      output: [
        { type: 'reasoning', summary: [{ type: 'summary_text', text: e }] },
        { type: 'message', content: [{ type: 'output_text', text: e }] },
        { type: 'function_call', arguments: `{"a":"\\u27e6EMAIL_1\\u27e7"}` },
      ],
    });
    expect(resp.output[0].summary[0].text).toBe(e);
    expect(resp.output[1].content[0].text).toBe(EMAIL);
    expect(resp.output[2].arguments).toBe(`{"a":"${EMAIL}"}`);
  });
});

describe('ChunkRestorer', () => {
  const setup = () => {
    const p = new Pseudonymisation();
    p.pseudonymiseChat([{ role: 'user', content: EMAIL }]);
    return new ChunkRestorer(p);
  };

  test('Responses deltas split inside a placeholder, flushed before done', () => {
    const r = setup();
    const ev = (delta: string) => ({
      type: 'response.output_text.delta',
      output_index: 0,
      content_index: 0,
      delta,
    });
    const out = [
      ...r.process(ev('to ')),
      ...r.process(ev(OPEN + 'EMA')),
      ...r.process(ev('IL_1' + CLOSE + ' and ' + OPEN + 'EM')),
      ...r.process({
        type: 'response.output_text.done',
        output_index: 0,
        content_index: 0,
        text: `to ${tok('EMAIL_1')} and ${OPEN}EM`,
      }),
    ];
    const deltas = out
      .filter((e) => e.type.endsWith('.delta'))
      .map((e) => e.delta)
      .join('');
    expect(deltas).toBe(`to ${EMAIL} and ${OPEN}EM`);
    expect(out[out.length - 1].text).toBe(`to ${EMAIL} and ${OPEN}EM`);
  });

  test('Responses completed event carries the restored response', () => {
    const r = setup();
    const [out] = r.process({
      type: 'response.completed',
      response: {
        output: [
          {
            type: 'message',
            content: [{ type: 'output_text', text: tok('EMAIL_1') }],
          },
        ],
      },
    });
    expect(out.response.output[0].content[0].text).toBe(EMAIL);
  });

  test('chat tool-call arguments held at finish get their own chunk', () => {
    const r = setup();
    const chunk = (args: string, finish: string | null = null) => ({
      choices: [
        {
          index: 0,
          delta: {
            tool_calls: [
              { index: 0, id: 'x', function: { name: 'f', arguments: args } },
            ],
          },
          finish_reason: finish,
        },
      ],
    });
    const a = r.process(chunk('{"to":"\\u27e6EMAIL_1'));
    const b = r.process({
      choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }],
    });
    const args = [...a, ...b]
      .flatMap((c) => c.choices)
      .map((c: any) => c.delta?.tool_calls?.[0]?.function?.arguments ?? '')
      .join('');
    // The tail never closed: it is released as it was.
    expect(args).toBe('{"to":"\\u27e6EMAIL_1');
    expect(b).toHaveLength(2);
    expect(b[0].choices[0].delta.tool_calls[0].id).toBeNull();
  });
});

describe('pyjson', () => {
  test('keeps key order, big integers and Python float repr', () => {
    expect(
      pyDumps(pyLoads('{"b":1,"1":[1.0,1e16,1e-5,12345678901234567890]}'))
    ).toBe('{"b": 1, "1": [1.0, 1e+16, 1e-05, 12345678901234567890]}');
  });
  test('refuses invalid JSON and very deep nesting', () => {
    for (const bad of ['[1,]', '{"a":1,}', '[01]', "{'a':1}", '', '[1 2]'])
      expect(() => pyLoads(bad)).toThrow(PyJsonError);
    expect(() => pyLoads('['.repeat(100_000) + ']'.repeat(100_000))).toThrow(
      PyJsonError
    );
  });
});

describe('linear time on hostile input (ReDoS)', () => {
  const fast = (fn: () => unknown, ms = 2000) => {
    const t0 = Date.now();
    fn();
    expect(Date.now() - t0).toBeLessThan(ms);
  };
  const p = new Pseudonymisation();
  p.pseudonymiseChat([{ role: 'user', content: EMAIL }]);
  const N = 200_000;

  test.each([
    ['open brackets', OPEN.repeat(N)],
    ['one open bracket, long name', OPEN + 'a_'.repeat(N)],
    ['many unterminated placeholders', (OPEN + 'EMAIL_1 ').repeat(N / 10)],
    ['spaces after a bracket', OPEN + ' '.repeat(N)],
    ['escaped brackets', '\\u27e6'.repeat(N / 6) + 'EMAIL_'],
  ])('restore: %s', (_name, text) => {
    fast(() => p.restore(text));
    fast(() => p.restore(text, true));
    fast(() => {
      const r = p.streamRestorer(true);
      for (let i = 0; i < text.length; i += 997) r.feed(text.slice(i, i + 997));
      r.flush();
    });
  });

  test('SSE: a long event in small chunks is not searched again and again', async () => {
    const event = `data: {"choices":[{"delta":{"content":"${'x'.repeat(2_000_000)} ${tok('EMAIL_1')}"}}]}\n\n`;
    const bytes = new TextEncoder().encode(event);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < bytes.length; i += 64)
          controller.enqueue(bytes.slice(i, i + 64));
        controller.close();
      },
    });
    const t0 = Date.now();
    const out = await new Response(restoreSSE(body, p, () => {})).text();
    // Quadratic (searching and copying the whole buffer per chunk) took
    // about 50 s here; linear takes well under a second when the host is idle.
    expect(Date.now() - t0).toBeLessThan(20000);
    expect(out.endsWith(` ${EMAIL}"}}]}\n\n`)).toBe(true);
  });

  test.each([
    ['letters', 'a'.repeat(N)],
    ['digit groups', '1 '.repeat(N / 2)],
    ['dashed digits', '1-'.repeat(N / 2)],
    ['at signs', 'a@'.repeat(N / 2)],
    ['dots', '1.'.repeat(N / 2)],
    ['emails', 'a@b.co '.repeat(N / 7)],
  ])('detection and replacement: %s', (_name, text) => {
    fast(() => findEntitySpans(text), 5000);
    fast(() => new Pseudonymisation().replace(text), 5000);
  });
});

describe('settings', () => {
  test('central section v1', () => {
    expect(parsePseudonymisationSection({ version: 1, enabled: true })).toEqual(
      {
        enabled: true,
        categories: undefined,
        onUnsupported: 'refuse',
      }
    );
    expect(
      parsePseudonymisationSection({
        version: 1,
        enabled: false,
        categories: ['email', 'email'],
        on_unsupported: 'passthrough',
      })
    ).toEqual({
      enabled: false,
      categories: ['email'],
      onUnsupported: 'passthrough',
    });
    for (const bad of [
      { version: 1 },
      { version: 1, enabled: 'yes' },
      { version: 1, enabled: true, categories: [] },
      { version: 1, enabled: true, categories: ['name'] },
      { version: 1, enabled: true, on_unsupported: 'allow' },
      { version: 1, enabled: true, key: 'x' },
    ])
      expect(() => parsePseudonymisationSection(bad)).toThrow();
  });

  test('env: off unless PS_PSEUDONYMISE, invalid values throw', () => {
    expect(pseudonymisationFromEnv({})).toBeUndefined();
    expect(
      pseudonymisationFromEnv({ PS_PSEUDONYMISE: 'false' })
    ).toBeUndefined();
    expect(
      pseudonymisationFromEnv({
        PS_PSEUDONYMISE: 'true',
        PS_PSEUDONYMISE_CATEGORIES: 'email, phone',
      })
    ).toEqual({
      enabled: true,
      categories: ['email', 'phone'],
      onUnsupported: 'refuse',
    });
    expect(() =>
      pseudonymisationFromEnv({
        PS_PSEUDONYMISE: '1',
        PS_PSEUDONYMISE_CATEGORIES: 'names',
      })
    ).toThrow();
    expect(() =>
      pseudonymisationFromEnv({
        PS_PSEUDONYMISE: '1',
        PS_PSEUDONYMISE_ON_UNSUPPORTED: 'x',
      })
    ).toThrow();
  });
});
