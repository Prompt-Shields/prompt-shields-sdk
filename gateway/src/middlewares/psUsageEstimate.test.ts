/**
 * Token estimates (#389): the shared vectors in schemas/stream-usage/vectors/
 * (also run by the SDK, packages/sdk/tests/test_usage_estimate.py), their
 * accuracy against the reference tokenizers, the streaming counter and the
 * request-body estimate.
 */
import * as fs from 'fs';
import * as path from 'path';
import {
  TokenCounter,
  estimateRequestTokens,
  estimateTextTokens,
  tokenFamily,
  TokenFamily,
} from './psUsageEstimate';

interface Vector {
  name: string;
  family: TokenFamily;
  text: string;
  expected: number;
  reference?: { tokenizer: string; tokens: number };
}

const VECTORS: Vector[] = JSON.parse(
  fs.readFileSync(
    path.join(
      __dirname,
      '../../../schemas/stream-usage/vectors/estimates.json'
    ),
    'utf8'
  )
).vectors;

describe('shared estimate vectors', () => {
  test.each(VECTORS.map((v) => [v.name, v] as const))('%s', (_n, v) => {
    expect(estimateTextTokens(v.text, v.family)).toBe(v.expected);
  });

  test.each(['o200k', 'cl100k'] as const)(
    '%s: within 10%% of the reference tokenizer over the fixture set',
    (family) => {
      const ref = VECTORS.filter((v) => v.family === family && v.reference);
      expect(ref.length).toBeGreaterThanOrEqual(20);
      const est = ref.reduce((s, v) => s + v.expected, 0);
      const real = ref.reduce((s, v) => s + v.reference!.tokens, 0);
      expect(Math.abs(est - real) / real).toBeLessThanOrEqual(0.1);
      // No single fixture is far off either.
      for (const v of ref) {
        const err = Math.abs(v.expected - v.reference!.tokens);
        expect(err / v.reference!.tokens).toBeLessThanOrEqual(0.3);
      }
    }
  );
});

describe('TokenCounter', () => {
  test('pieces add up to the whole text, for each family', () => {
    const text = VECTORS.filter((v) => v.reference)
      .map((v) => v.text)
      .join(' ')
      .repeat(3);
    const counter = new TokenCounter();
    let seed = 7;
    for (let i = 0; i < text.length; ) {
      seed = (seed * 48271) % 2147483647;
      const n = 1 + (seed % 9);
      counter.feed(text.slice(i, i + n));
      i += n;
    }
    for (const f of ['o200k', 'cl100k'] as const)
      expect(counter.tokens(f)).toBe(estimateTextTokens(text, f));
  });

  test('ignores non-strings', () => {
    const counter = new TokenCounter();
    counter.feed(undefined);
    counter.feed(42);
    expect(counter.tokens('o200k')).toBe(0);
  });
});

test.each([
  ['gpt-4o-2024-08-06', undefined, 'o200k'],
  ['openai/gpt-5-mini', undefined, 'o200k'],
  ['o3-mini', undefined, 'o200k'],
  ['gemini-2.5-flash', undefined, 'o200k'],
  ['gpt-4-turbo', undefined, 'cl100k'],
  ['claude-sonnet-4-20250514', 'anthropic', 'cl100k'],
  ['llama-3', undefined, 'cl100k'],
  [undefined, undefined, 'cl100k'],
])('tokenFamily(%s, %s) = %s', (model, vendor, family) => {
  expect(tokenFamily(model, vendor)).toBe(family);
});

describe('estimateRequestTokens', () => {
  const hi = estimateTextTokens('Say hi to the team', 'o200k');

  test('chat messages: text plus framing', () => {
    expect(
      estimateRequestTokens(
        { messages: [{ role: 'user', content: 'Say hi to the team' }] },
        'o200k'
      )
    ).toBe(hi + 3 + 3);
  });

  test('content parts, images and tool calls', () => {
    const body = {
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'Say hi to the team' },
            { type: 'image_url', image_url: { url: 'data:...' } },
          ],
        },
        {
          role: 'assistant',
          tool_calls: [
            { function: { name: 'greet', arguments: '{"who":"team"}' } },
          ],
        },
      ],
    };
    expect(estimateRequestTokens(body, 'o200k')).toBeGreaterThan(hi + 1600 + 6);
  });

  test('Messages system, Responses input and Completions prompt', () => {
    expect(
      estimateRequestTokens(
        { system: 'Be brief.', messages: [{ role: 'user', content: 'Hi' }] },
        'cl100k'
      )
    ).toBeGreaterThan(6);
    expect(
      estimateRequestTokens({ input: 'Say hi to the team' }, 'o200k')
    ).toBe(hi + 3 + 3);
    expect(
      estimateRequestTokens({ prompt: 'Say hi to the team' }, 'o200k')
    ).toBe(hi + 3);
  });

  test('a body without text is not estimated', () => {
    expect(estimateRequestTokens({ model: 'x' })).toBeUndefined();
    expect(estimateRequestTokens(undefined)).toBeUndefined();
  });
});
