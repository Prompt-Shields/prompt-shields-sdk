/**
 * Minimal Prometheus text-format registry. Dependency-free on purpose: the
 * gateway also builds for Cloudflare Workers, where prom-client cannot run.
 * Upstream Portkey 2.0.0 uses prom-client; this is a PS replacement for #61.
 */

type Labels = Record<string, string>;

const OVERFLOW = '__overflow__';
const escapeValue = (v: string) =>
  v.replace(/\\/g, '\\\\').replace(/\n/g, '\\n').replace(/"/g, '\\"');

const seriesKey = (labelNames: string[], labels: Labels) =>
  labelNames.map((n) => labels[n] ?? '').join('\u0000');

const renderLabels = (labelNames: string[], key: string, extra = '') => {
  const values = key.split('\u0000');
  const parts = labelNames.map((n, i) => `${n}="${escapeValue(values[i])}"`);
  if (extra) parts.push(extra);
  return parts.length ? `{${parts.join(',')}}` : '';
};

abstract class Metric {
  protected keys = new Map<string, unknown>();
  constructor(
    readonly name: string,
    readonly help: string,
    readonly labelNames: string[],
    // Cardinality guard: once this many series exist, new label sets are
    // folded into one overflow series instead of growing memory forever.
    readonly maxSeries = 1000
  ) {}

  protected keyFor(labels: Labels): string {
    const key = seriesKey(this.labelNames, labels);
    if (this.keys.has(key) || this.keys.size < this.maxSeries) return key;
    return this.labelNames.map(() => OVERFLOW).join('\u0000');
  }

  abstract render(): string;
}

export class Counter extends Metric {
  inc(labels: Labels, value = 1) {
    if (!(value >= 0)) return;
    const key = this.keyFor(labels);
    this.keys.set(key, ((this.keys.get(key) as number) ?? 0) + value);
  }

  render() {
    const lines = [
      `# HELP ${this.name} ${this.help}`,
      `# TYPE ${this.name} counter`,
    ];
    for (const [key, value] of this.keys) {
      lines.push(`${this.name}${renderLabels(this.labelNames, key)} ${value}`);
    }
    return lines.join('\n');
  }
}

type HistogramState = { buckets: number[]; sum: number; count: number };

export class Histogram extends Metric {
  constructor(
    name: string,
    help: string,
    labelNames: string[],
    readonly bucketBounds: number[] = [
      0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60,
    ],
    maxSeries?: number
  ) {
    super(name, help, labelNames, maxSeries);
  }

  observe(labels: Labels, value: number) {
    if (!Number.isFinite(value)) return;
    const key = this.keyFor(labels);
    let state = this.keys.get(key) as HistogramState | undefined;
    if (!state) {
      state = { buckets: this.bucketBounds.map(() => 0), sum: 0, count: 0 };
      this.keys.set(key, state);
    }
    this.bucketBounds.forEach((bound, i) => {
      if (value <= bound) state!.buckets[i]++;
    });
    state.sum += value;
    state.count++;
  }

  render() {
    const lines = [
      `# HELP ${this.name} ${this.help}`,
      `# TYPE ${this.name} histogram`,
    ];
    for (const [key, raw] of this.keys) {
      const state = raw as HistogramState;
      this.bucketBounds.forEach((bound, i) => {
        lines.push(
          `${this.name}_bucket${renderLabels(this.labelNames, key, `le="${bound}"`)} ${state.buckets[i]}`
        );
      });
      lines.push(
        `${this.name}_bucket${renderLabels(this.labelNames, key, 'le="+Inf"')} ${state.count}`
      );
      lines.push(
        `${this.name}_sum${renderLabels(this.labelNames, key)} ${state.sum}`
      );
      lines.push(
        `${this.name}_count${renderLabels(this.labelNames, key)} ${state.count}`
      );
    }
    return lines.join('\n');
  }
}

export class Registry {
  private metrics: Metric[] = [];
  register<T extends Metric>(metric: T): T {
    this.metrics.push(metric);
    return metric;
  }
  render(): string {
    return this.metrics.map((m) => m.render()).join('\n\n') + '\n';
  }
}

/** The registry `/metrics` renders; every gateway metric registers here. */
export const defaultRegistry = new Registry();

export const PROMETHEUS_CONTENT_TYPE =
  'text/plain; version=0.0.4; charset=utf-8';
