/**
 * findPiiSpans / redactPii (#337): the span-reporting twin of
 * detectPiiCategories, used to redact the admin log stream.
 */
import * as fs from 'fs';
import * as path from 'path';
import { detectPiiCategories, findPiiSpans, redactPii } from './pii';

const KEYWORD_CATEGORIES = new Set(['health_data', 'financial_data']);
const patternCategories = (text: string) =>
  detectPiiCategories(text).filter((c) => !KEYWORD_CATEGORIES.has(c));

describe('findPiiSpans', () => {
  test.each([
    ['email', 'mail jane.doe@example.com now', 'jane.doe@example.com'],
    ['credit_card', 'card 4111 1111 1111 1111 ok', '4111 1111 1111 1111'],
    ['credit_card', 'card 4111-1111-1111-1111.', '4111-1111-1111-1111'],
    ['ssn', 'ssn 123-45-6789 here', '123-45-6789'],
    ['ip_address', 'from 192.168.10.20 today', '192.168.10.20'],
    ['phone', 'call 555-123-4567 please', '555-123-4567'],
    ['phone', 'ring +46 70 174 06 12 now', '+46 70 174 06 12'],
    [
      'iban',
      'pay GB82 WEST 1234 5698 7654 32 thanks',
      'GB82 WEST 1234 5698 7654 32',
    ],
    ['national_id', 'personnummer 19121212-1212 ok', '19121212-1212'],
  ])('%s span covers the value', (category, text, value) => {
    const spans = findPiiSpans(text);
    expect(spans).toHaveLength(1);
    const [span] = spans;
    expect(span.category).toBe(category);
    expect(text.slice(span.start, span.end)).toBe(value);
  });

  test('no spans in clean text, keywords alone, or empty input', () => {
    expect(findPiiSpans('Summarise the quarterly report')).toEqual([]);
    expect(findPiiSpans('the diagnosis and account number fields')).toEqual([]);
    expect(findPiiSpans('')).toEqual([]);
    expect(findPiiSpans(undefined)).toEqual([]);
  });

  test('a card right after an IBAN is still found, and not hidden in it', () => {
    const text = 'GB82 WEST 1234 5698 7654 32 4111 1111 1111 1111';
    expect(findPiiSpans(text).map((s) => s.category)).toEqual([
      'iban',
      'credit_card',
    ]);
  });

  test('spans are sorted and disjoint', () => {
    const text =
      'a@b.io 4111111111111111 555-123-4567 10.0.0.1 x@y.org 123-45-6789';
    const spans = findPiiSpans(text);
    expect(spans.length).toBeGreaterThanOrEqual(6);
    for (let i = 1; i < spans.length; i++)
      expect(spans[i].start).toBeGreaterThanOrEqual(spans[i - 1].end);
  });
});

describe('redactPii', () => {
  test('replaces values with category placeholders', () => {
    expect(
      redactPii('Email jane@example.com, card 4111 1111 1111 1111, thanks')
    ).toBe('Email [EMAIL], card [CREDIT_CARD], thanks');
  });

  test('leaves text without PII unchanged', () => {
    const text = 'Model gpt-4o, 128 tokens, 2026-11-06';
    expect(redactPii(text)).toBe(text);
  });

  test('a long run of letters stays fast', () => {
    const text = 'a'.repeat(100_000) + ' end';
    const t0 = Date.now();
    expect(redactPii(text)).toBe(text);
    expect(Date.now() - t0).toBeLessThan(2000);
  });

  // Every example of the shared PII benchmark: spans report exactly the
  // pattern categories detectPiiCategories finds, and after redaction the
  // detector finds none of them.
  test('agrees with detectPiiCategories on the PII benchmark dataset', () => {
    const dir = path.resolve(__dirname, '../../../../benchmarks/pii');
    const texts: string[] = fs
      .readFileSync(path.join(dir, 'dataset.v1.jsonl'), 'utf8')
      .split('\n')
      .filter((l) => l.trim())
      .map((l) => JSON.parse(l).text);
    expect(texts.length).toBeGreaterThan(0);
    const mismatched: string[] = [];
    const leaked: string[] = [];
    for (const text of texts) {
      const spanCategories = [
        ...new Set(findPiiSpans(text).map((s) => s.category)),
      ].sort();
      const detected = patternCategories(text);
      // Merged overlaps keep one category, so spans may report fewer.
      if (spanCategories.some((c) => !detected.includes(c)))
        mismatched.push(text);
      if (detected.length && !spanCategories.length) mismatched.push(text);
      if (patternCategories(redactPii(text)).length) leaked.push(text);
    }
    expect(mismatched).toEqual([]);
    expect(leaked).toEqual([]);
  });
});
