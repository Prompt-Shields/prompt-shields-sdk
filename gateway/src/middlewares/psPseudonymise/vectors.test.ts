/**
 * Cross-language vectors (#350): the gateway mints exactly the placeholders
 * the SDK mints. schemas/pseudonymisation/vectors.v1.json is generated from
 * packages/sdk/prompt_shields/pseudonymise.py and checked by the SDK too
 * (packages/sdk/tests/test_pseudonymise_vectors.py).
 */
import * as fs from 'fs';
import * as path from 'path';
import { Pseudonymisation } from './core';

const ROOT = path.resolve(__dirname, '../../../..');
const VECTORS = JSON.parse(
  fs.readFileSync(
    path.join(ROOT, 'schemas/pseudonymisation/vectors.v1.json'),
    'utf8'
  )
);
const CASES: Record<string, any> = Object.fromEntries(
  VECTORS.pseudonymise.map((c: any) => [c.name, c])
);
const PSEUDO: [string, any][] = VECTORS.pseudonymise.map((c: any) => [
  c.name,
  c,
]);
const STREAM: [string, any][] = VECTORS.stream.map((c: any) => [c.name, c]);

function run(c: any) {
  const p = new Pseudonymisation(c.categories);
  const out = p.pseudonymiseChat(c.messages, c.system, c.prediction);
  return { p, out };
}

describe('pseudonymisation vectors (shared with the SDK)', () => {
  test.each(PSEUDO)('%s', (_name, c) => {
    const { p, out } = run(c);
    expect(out.messages).toEqual(c.expected.messages);
    if ('system' in c.expected) expect(out.system).toEqual(c.expected.system);
    if ('prediction' in c.expected)
      expect(out.prediction).toEqual(c.expected.prediction);
    expect(p.stats().entities).toEqual(c.expected.entities);
  });

  test.each(PSEUDO)('request body for its surface: %s', (_name, c) => {
    const p = new Pseudonymisation(c.categories);
    const body: any = { model: 'm', messages: c.messages };
    if (c.surface === 'messages' && 'system' in c) body.system = c.system;
    if (c.surface === 'chat' && 'prediction' in c)
      body.prediction = c.prediction;
    const before = JSON.stringify(body);
    const out = p.pseudonymiseBody(c.surface, body);
    expect(JSON.stringify(body)).toBe(before); // the input is not changed
    expect(out.messages).toEqual(c.expected.messages);
    if ('system' in c.expected) expect(out.system).toEqual(c.expected.system);
    if ('prediction' in c.expected)
      expect(out.prediction).toEqual(c.expected.prediction);
    expect(out.model).toBe('m');
  });

  test('restore', () => {
    const { p } = run(CASES[VECTORS.restore[0].mapping_from]);
    for (const c of VECTORS.restore)
      expect([c.name, p.restore(c.text, c.json_escape)]).toEqual([
        c.name,
        c.expected,
      ]);
    const { restored, unknown } = p.stats();
    expect({ restored, unknown }).toEqual(VECTORS.restore_stats);
  });

  test.each(STREAM)('stream: %s', (_name, c) => {
    const { p } = run(CASES[c.mapping_from]);
    const r = p.streamRestorer(c.json_escape);
    expect(c.pieces.map((piece: string) => r.feed(piece))).toEqual(c.outputs);
    expect(r.flush()).toBe(c.flush);
  });

  test('every PII benchmark example is pseudonymised as the SDK does it', () => {
    const examples = fs
      .readFileSync(path.join(ROOT, 'benchmarks/pii/dataset.v1.jsonl'), 'utf8')
      .split('\n')
      .filter((l) => l.trim())
      .map((l) => JSON.parse(l));
    expect(examples.length).toBeGreaterThan(0);
    const mismatched: string[] = [];
    for (const ex of examples) {
      const [msg] = new Pseudonymisation().pseudonymiseChat([
        { role: 'user', content: ex.text },
      ]).messages;
      if (msg.content !== (VECTORS.dataset[ex.id] ?? ex.text))
        mismatched.push(ex.id);
    }
    expect(mismatched).toEqual([]);
  });
});
