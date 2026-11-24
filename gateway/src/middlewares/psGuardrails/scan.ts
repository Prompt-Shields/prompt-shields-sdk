/**
 * PS fork (#349): applying guardrail rules to text.
 *
 * `prepareStage` picks the detectors a stage's rules need; `evaluate` runs
 * them and gives each finding the strictest matching rule (deny > redact >
 * log); `redact` replaces the redact hits with placeholders. The Tally
 * counts hits per rule and category for security events and telemetry,
 * never the matched text.
 */
import { piiPlaceholder } from '../router/pii';
import {
  ACTION_RANK,
  GuardrailAction,
  GuardrailRule,
  GuardrailStageName,
  PII_CONTEXT_CATEGORIES,
} from './config';
import { detect, DetectorSelection, Finding } from './detectors';

export interface StageRules {
  stage: GuardrailStageName;
  rules: GuardrailRule[];
  sel: DetectorSelection;
}

export interface Hit extends Finding {
  rule: GuardrailRule;
  action: GuardrailAction;
}

export function prepareStage(
  stage: GuardrailStageName,
  rules: GuardrailRule[]
): StageRules | undefined {
  if (!rules.length) return undefined;
  const context = PII_CONTEXT_CATEGORIES as readonly string[];
  const sel: DetectorSelection = {
    pii: false,
    piiContext: false,
    secrets: false,
    injection: false,
  };
  for (const r of rules) {
    if (r.check === 'pii') {
      for (const c of r.categories)
        if (context.includes(c)) sel.piiContext = true;
        else sel.pii = true;
    } else sel[r.check] = true;
  }
  return { stage, rules, sel };
}

/** Findings in `text` with the rule that governs each (none: dropped). */
export function evaluate(text: string, stage: StageRules): Hit[] {
  const hits: Hit[] = [];
  for (const f of detect(text, stage.sel)) {
    let best: GuardrailRule | undefined;
    for (const r of stage.rules)
      if (
        r.check === f.check &&
        r.categories.has(f.category) &&
        (!best || ACTION_RANK[r.action] > ACTION_RANK[best.action])
      )
        best = r;
    if (best) hits.push({ ...f, rule: best, action: best.action });
  }
  return hits;
}

/** Placeholder for a redacted finding: `[EMAIL]`, `[OPENAI_KEY]`, ... */
export const placeholder = (category: string) => piiPlaceholder(category);

/** The redact hits as sorted, disjoint spans (the first category kept). */
export function redactSpans(
  hits: Hit[]
): { start: number; end: number; category: string }[] {
  const spans = hits
    .filter((h) => h.action === 'redact' && h.start !== undefined)
    .map((h) => ({ start: h.start!, end: h.end!, category: h.category }))
    .sort((a, b) => a.start - b.start || b.end - a.end);
  const merged: typeof spans = [];
  for (const s of spans) {
    const last = merged[merged.length - 1];
    if (last && s.start < last.end) last.end = Math.max(last.end, s.end);
    else merged.push({ ...s });
  }
  return merged;
}

/** `text` with every redact hit replaced by its placeholder. */
export function redact(text: string, hits: Hit[]): string {
  const spans = redactSpans(hits);
  if (!spans.length) return text;
  let out = '';
  let pos = 0;
  for (const { start, end, category } of spans) {
    out += text.slice(pos, start) + placeholder(category);
    pos = end;
  }
  return out + text.slice(pos);
}

export interface TallyEntry {
  stage: GuardrailStageName;
  rule: GuardrailRule;
  category: string;
  action: GuardrailAction;
  count: number;
}

/** Hits per stage, rule and category; bounded by the rules' categories. */
export class Tally {
  readonly entries = new Map<string, TallyEntry>();
  denied?: TallyEntry;

  add(stage: GuardrailStageName, hit: Hit, n = 1): TallyEntry {
    const key = `${stage}|${hit.rule.key}|${hit.category}`;
    let e = this.entries.get(key);
    if (!e) {
      e = {
        stage,
        rule: hit.rule,
        category: hit.category,
        action: hit.action,
        count: 0,
      };
      this.entries.set(key, e);
    }
    e.count += n;
    if (hit.action === 'deny' && !this.denied) this.denied = e;
    return e;
  }

  /** PII categories found, sorted (for telemetry's detected_pii_types). */
  piiCategories(stage?: GuardrailStageName): string[] {
    const out = new Set<string>();
    for (const e of this.entries.values())
      if (e.rule.check === 'pii' && (!stage || e.stage === stage))
        out.add(e.category);
    return [...out].sort();
  }
}

// Response keys whose string values are model output: chat `content`,
// Anthropic / Gemini / Bedrock `text`, Responses `output_text`, tool-call
// `arguments` (and tool `input` objects), refusals and reasoning text.
const OUTPUT_TEXT_KEYS = new Set([
  'content',
  'text',
  'output_text',
  'arguments',
  'refusal',
  'reasoning_content',
  'thinking',
  'transcript',
  'partial_json',
]);
const OUTPUT_ARG_KEYS = new Set(['input', 'arguments', 'args']);
const OUTPUT_SKIP_KEYS = new Set([
  'b64_json',
  'data',
  'audio',
  'image_url',
  'url',
  'logprobs',
  'usage',
  'embedding',
  'signature',
]);

/**
 * Calls `visit(path, text)` on every model-output string of a response
 * object (or SSE event), storing what it returns in place. `path` names
 * the string's position (`choices.0.delta.content`): an array element that
 * is an object with a numeric `index` (streamed choices and tool calls)
 * is named by that index rather than its position, so the deltas of one
 * choice share a path across SSE events.
 */
export function mapOutputText(
  node: unknown,
  visit: (path: string, text: string) => string
): void {
  const walk = (
    n: unknown,
    path: string,
    key: string,
    all: boolean,
    depth: number
  ) => {
    if (depth > 32 || n == null || typeof n !== 'object') return;
    const isArray = Array.isArray(n);
    const entries: [string | number, unknown][] = isArray
      ? (n as unknown[]).map((v, i) => [i, v])
      : Object.entries(n as Record<string, unknown>);
    for (const [k, v] of entries) {
      const childKey = isArray ? key : (k as string);
      if (!isArray && OUTPUT_SKIP_KEYS.has(childKey)) continue;
      const step =
        isArray &&
        v &&
        typeof v === 'object' &&
        typeof (v as any).index === 'number'
          ? (v as any).index
          : k;
      const p = path ? `${path}.${step}` : String(step);
      if (typeof v === 'string') {
        if (all || OUTPUT_TEXT_KEYS.has(childKey)) {
          const next = visit(p, v);
          if (next !== v) (n as any)[k] = next;
        }
      } else {
        const args =
          !isArray &&
          OUTPUT_ARG_KEYS.has(childKey) &&
          typeof v === 'object' &&
          !Array.isArray(v);
        walk(v, p, childKey, all || args, depth + 1);
      }
    }
  };
  walk(node, '', '', false, 0);
}
