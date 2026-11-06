/**
 * Labelled Nordic/EU PII benchmark and recall gate (#35) for the gateway's
 * port of the SDK detector.
 *
 * The synthetic dataset, its generator, the thresholds and the Python scorer
 * live in benchmarks/pii/ and are shared with the SDK
 * (packages/sdk/tests/test_pii_benchmark.py). Only data is shared; scoring
 * here mirrors benchmarks/pii/score.py: per example and per category, the
 * way the detector reports (category presence, never spans).
 *
 * PS_PII_BENCH_REPORT=1 npx jest piiBenchmark prints the report.
 */
import * as fs from 'fs';
import * as path from 'path';
import { detectPiiCategories } from './pii';

const DIR = path.resolve(__dirname, '../../../../benchmarks/pii');

interface Example {
  id: string;
  locale: string;
  text: string;
  labels: string[];
}
interface Counts {
  tp: number;
  fp: number;
  fn: number;
}

const records: Example[] = fs
  .readFileSync(path.join(DIR, 'dataset.v1.jsonl'), 'utf8')
  .split('\n')
  .filter((l) => l.trim())
  .map((l) => JSON.parse(l));
const thresholds: { min_recall: Record<string, number> } = JSON.parse(
  fs.readFileSync(path.join(DIR, 'thresholds.json'), 'utf8')
);

function score(): Record<string, Counts> {
  const counts: Record<string, Counts> = {};
  const bump = (key: string, field: keyof Counts) => {
    counts[key] ??= { tp: 0, fp: 0, fn: 0 };
    counts[key][field]++;
  };
  for (const rec of records) {
    const expected = new Set(rec.labels);
    const predicted = new Set(detectPiiCategories(rec.text));
    for (const cat of new Set([...expected, ...predicted])) {
      const field: keyof Counts =
        expected.has(cat) && predicted.has(cat)
          ? 'tp'
          : predicted.has(cat)
            ? 'fp'
            : 'fn';
      bump(cat, field);
      bump(`${cat}@${rec.locale}`, field);
    }
  }
  return counts;
}

const recall = (c?: Counts) =>
  !c || c.tp + c.fn === 0 ? null : c.tp / (c.tp + c.fn);
const precision = (c?: Counts) =>
  !c || c.tp + c.fp === 0 ? null : c.tp / (c.tp + c.fp);
const fmt = (x: number | null) => (x === null ? '-' : x.toFixed(3));

describe('PII benchmark (gateway detector)', () => {
  const counts = score();

  test('the dataset is non-trivial', () => {
    expect(records.length).toBeGreaterThan(500);
    expect(records.some((r) => r.labels.length === 0)).toBe(true);
  });

  test.each(Object.entries(thresholds.min_recall))(
    'recall gate: %s >= %d',
    (key, minimum) => {
      const r = recall(counts[key]);
      expect(r).not.toBeNull();
      expect(r! + 1e-9).toBeGreaterThanOrEqual(minimum);
    }
  );

  if (process.env.PS_PII_BENCH_REPORT) {
    test('report', () => {
      const rows = Object.keys(counts)
        .filter((k) => !k.includes('@'))
        .sort()
        .map((k) => {
          const c = counts[k];
          const p = precision(c);
          const r = recall(c);
          const f1 =
            p === null || r === null || p + r === 0
              ? null
              : (2 * p * r) / (p + r);
          return `| ${k} | ${c.tp + c.fn} | ${c.tp} | ${c.fp} | ${c.fn} | ${fmt(p)} | ${fmt(r)} | ${fmt(f1)} |`;
        });
      // eslint-disable-next-line no-console
      console.log(
        [
          `### Gateway (router/pii.ts), ${records.length} examples`,
          '',
          '| category | support | TP | FP | FN | precision | recall | F1 |',
          '|---|---:|---:|---:|---:|---:|---:|---:|',
          ...rows,
        ].join('\n')
      );
    });
  }
});
