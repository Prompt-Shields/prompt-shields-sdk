/**
 * PS fork (#389): token estimates for streams whose provider reported no
 * usage (cancelled or cut streams, providers that never send it). Events
 * and spans built from them carry `usage_source: "estimated"`.
 *
 * The same algorithm as the SDK's `prompt_shields/usage_estimate.py`,
 * specified in `schemas/stream-usage/README.md`; both pass the shared
 * vectors in `schemas/stream-usage/vectors/`. It approximates a byte-pair
 * tokenizer without its vocabulary: the text is split the way a GPT-style
 * pre-tokenizer would split it and each piece is costed with per-family
 * parameters fitted to OpenAI's `o200k_base` and `cl100k_base`. All
 * arithmetic is in integer hundredths of a token, so both implementations
 * return the same number for the same text.
 */

export type TokenFamily = 'o200k' | 'cl100k';

interface FamilyParams {
  wordFree: number;
  wordStep: number;
  latin: number;
  alpha: number;
  cjk: number;
  hangul: number;
  cjkPunct: number;
  symbol: number;
  punctRun: number;
  digitRun: number;
  glued: number;
}

/** Per-family costs, in hundredths of a token (see the README). */
export const FAMILY_PARAMS: Record<TokenFamily, FamilyParams> = {
  o200k: {
    wordFree: 8,
    wordStep: 3,
    latin: 15,
    alpha: 21,
    cjk: 65,
    hangul: 65,
    cjkPunct: 50,
    symbol: 150,
    punctRun: 3,
    digitRun: 3,
    glued: 50,
  },
  cl100k: {
    wordFree: 8,
    wordStep: 3,
    latin: 49,
    alpha: 42,
    cjk: 105,
    hangul: 130,
    cjkPunct: 50,
    symbol: 175,
    punctRun: 3,
    digitRun: 3,
    glued: 50,
  },
};

export const MESSAGE_FRAMING_TOKENS = 3;
export const REPLY_FRAMING_TOKENS = 3;
export const MEDIA_TOKENS = 1600;

const FLUSH_CHARS = 4096;
const MAX_BUFFER_CHARS = 65536;

const O200K_PREFIXES = [
  'gpt-4o',
  'gpt-4.1',
  'gpt-4.5',
  'gpt-5',
  'chatgpt-',
  'o1',
  'o3',
  'o4',
  'gpt-oss',
];

/** The estimate family for a model. */
export function tokenFamily(model?: string, vendor?: string): TokenFamily {
  let m = (model || '').toLowerCase();
  if (m.includes('/')) m = m.slice(m.lastIndexOf('/') + 1);
  if (O200K_PREFIXES.some((p) => m.startsWith(p))) return 'o200k';
  const v = (vendor || '').toLowerCase();
  if (m.startsWith('gemini') || v === 'google' || v === 'vertex-ai')
    return 'o200k';
  return 'cl100k';
}

const SPACES = new Set<number>([
  9, 10, 11, 12, 13, 32, 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f,
  0x3000,
]);
for (let o = 0x2000; o <= 0x200a; o++) SPACES.add(o);

type Cls =
  | 'sp'
  | 'dig'
  | 'asc'
  | 'pun'
  | 'lat'
  | 'alp'
  | 'sym'
  | 'han'
  | 'cjp'
  | 'cjk';

function cls(o: number): Cls {
  if (SPACES.has(o)) return 'sp';
  if (o >= 48 && o <= 57) return 'dig';
  if ((o >= 65 && o <= 90) || (o >= 97 && o <= 122)) return 'asc';
  if (o < 128) return 'pun';
  if (o >= 0xc0 && o <= 0x24f && o !== 0xd7 && o !== 0xf7) return 'lat';
  if (o >= 0x370 && o <= 0x1fff) return 'alp';
  if (o >= 0x2000 && o <= 0x2bff) return 'sym';
  if (
    (o >= 0x1100 && o <= 0x11ff) ||
    (o >= 0x3130 && o <= 0x318f) ||
    (o >= 0xac00 && o <= 0xd7af)
  )
    return 'han';
  if (
    (o >= 0x3000 && o <= 0x303f) ||
    (o >= 0xff01 && o <= 0xff0f) ||
    (o >= 0xff1a && o <= 0xff20)
  )
    return 'cjp';
  if (
    (o >= 0x2e80 && o <= 0x9fff) ||
    (o >= 0xf900 && o <= 0xfaff) ||
    (o >= 0xff00 && o <= 0xffef)
  )
    return 'cjk';
  if (o >= 0x1f000 || (o >= 0xfe00 && o <= 0xfe0f)) return 'sym';
  if (o < 0x370) return 'pun';
  return 'alp';
}

const isWord = (c: Cls) => c === 'asc' || c === 'lat' || c === 'alp';

interface Run {
  c: Cls | 'word';
  cps: number[];
}

function runsOf(text: string): Run[] {
  const cps: number[] = [];
  for (const ch of text) cps.push(ch.codePointAt(0)!);
  const out: Run[] = [];
  let i = 0;
  while (i < cps.length) {
    const c = cls(cps[i]);
    let j = i + 1;
    if (isWord(c)) {
      while (j < cps.length && isWord(cls(cps[j]))) j++;
      out.push({ c: 'word', cps: cps.slice(i, j) });
    } else {
      while (j < cps.length && cls(cps[j]) === c) j++;
      out.push({ c, cps: cps.slice(i, j) });
    }
    i = j;
  }
  return out;
}

const isLower = (o: number) => o >= 97 && o <= 122;
const isUpper = (o: number) => o >= 65 && o <= 90;
const ceilDiv = (a: number, b: number) => Math.floor((a + b - 1) / b);
const NL = 10;
const CR = 13;
const SPACE = 32;
const TAB = 9;

/** Estimated tokens of `text`, in hundredths of a token. */
export function textHundredths(
  text: string,
  family: TokenFamily = 'cl100k'
): number {
  const p = FAMILY_PARAMS[family] ?? FAMILY_PARAMS.cl100k;
  const runs = runsOf(text);
  let total = 0;
  for (let idx = 0; idx < runs.length; idx++) {
    const { c, cps } = runs[idx];
    const n = cps.length;
    const prev = idx > 0 ? runs[idx - 1].c : undefined;
    const next = idx + 1 < runs.length ? runs[idx + 1].c : undefined;
    if (c === 'word') {
      const kinds = new Set(cps.map(cls));
      if (kinds.size === 1 && kinds.has('asc')) {
        let start = 0;
        const parts: number[] = [];
        for (let k = 1; k < n; k++) {
          if (isLower(cps[k - 1]) && isUpper(cps[k])) {
            parts.push(k - start);
            start = k;
          }
        }
        parts.push(n - start);
        for (const k of parts) {
          total +=
            k <= p.wordFree
              ? 100
              : 100 + 100 * ceilDiv(k - p.wordFree, p.wordStep);
        }
      } else if (kinds.has('alp')) {
        total += Math.max(100, n * p.alpha);
      } else {
        total += Math.max(100, n * p.latin);
      }
    } else if (c === 'dig') {
      total += 100 * ceilDiv(n, p.digitRun);
    } else if (c === 'pun') {
      if (n === 1 && next === 'word') total += prev === 'sp' ? 100 : p.glued;
      else total += 100 * ceilDiv(n, p.punctRun);
    } else if (c === 'cjk') {
      total += n * p.cjk;
    } else if (c === 'han') {
      total += n * p.hangul;
    } else if (c === 'cjp') {
      total += n * p.cjkPunct;
    } else if (c === 'sym') {
      total += n * p.symbol;
    } else {
      // whitespace
      let r = cps;
      const lastCp = r[r.length - 1];
      if (
        (next !== undefined && next !== 'dig' && lastCp === SPACE) ||
        (next === 'word' && lastCp === TAB)
      ) {
        r = r.slice(0, -1); // the last space joins the next piece
      }
      if (!r.length) continue;
      if (prev === 'pun') {
        let cut = -1;
        for (let k = r.length - 1; k >= 0; k--) {
          if (r[k] === NL || r[k] === CR) {
            cut = k;
            break;
          }
        }
        if (cut >= 0) {
          r = r.slice(cut + 1); // newlines join the punctuation before
          if (!r.length) continue;
        }
      }
      total += 100;
    }
  }
  return total;
}

const round = (hundredths: number) => Math.floor((hundredths + 50) / 100);

/** Estimated tokens of `text` for a model family. */
export function estimateTextTokens(
  text: string,
  family: TokenFamily = 'cl100k'
): number {
  if (!text) return 0;
  return round(textHundredths(text, family));
}

/**
 * Index of a lone space between two non-space characters, from the end.
 * Estimating the text on each side separately gives the same total as
 * estimating it whole. Works on UTF-16 indices: a space and its neighbours
 * checked here are never surrogate halves.
 */
function safeCut(buf: string): number {
  for (let i = buf.length - 2; i > 0; i--) {
    if (
      buf.charCodeAt(i) === SPACE &&
      !SPACES.has(buf.charCodeAt(i - 1)) &&
      !SPACES.has(buf.charCodeAt(i + 1))
    )
      return i;
  }
  return -1;
}

const FAMILIES: TokenFamily[] = ['o200k', 'cl100k'];

/**
 * Estimates the tokens of text that arrives in pieces (stream deltas),
 * for every family at once, keeping at most a few KB. The total equals
 * `estimateTextTokens` of the joined text, except for a buffer that grows
 * past 64K characters with no space in it.
 */
export class TokenCounter {
  private done: Record<TokenFamily, number> = { o200k: 0, cl100k: 0 };
  private buf = '';

  feed(text: unknown): void {
    if (typeof text !== 'string' || !text) return;
    this.buf += text;
    if (this.buf.length < FLUSH_CHARS) return;
    let cut = safeCut(this.buf);
    if (cut <= 0) {
      if (this.buf.length < MAX_BUFFER_CHARS) return;
      cut = this.buf.length;
    }
    const head = this.buf.slice(0, cut);
    this.buf = this.buf.slice(cut);
    for (const f of FAMILIES) this.done[f] += textHundredths(head, f);
  }

  tokens(family: TokenFamily): number {
    return round(this.done[family] + textHundredths(this.buf, family));
  }
}

function json(value: unknown): string {
  try {
    return JSON.stringify(value) ?? '';
  } catch {
    return '';
  }
}

const MEDIA_PARTS = new Set([
  'image',
  'image_url',
  'input_image',
  'input_audio',
  'input_file',
  'file',
  'document',
]);

function contentHundredths(content: unknown, family: TokenFamily): number {
  if (typeof content === 'string') return textHundredths(content, family);
  if (!Array.isArray(content)) return 0;
  let total = 0;
  for (const part of content) {
    if (typeof part === 'string') {
      total += textHundredths(part, family);
      continue;
    }
    if (!part || typeof part !== 'object') continue;
    if (typeof part.text === 'string') {
      total += textHundredths(part.text, family);
    } else if (MEDIA_PARTS.has(part.type)) {
      total += MEDIA_TOKENS * 100;
    } else if (part.type === 'tool_use' || part.type === 'tool_result') {
      total +=
        part.content !== undefined
          ? contentHundredths(part.content, family)
          : textHundredths(json(part.input ?? {}), family);
    } else if (typeof part.thinking === 'string') {
      total += textHundredths(part.thinking, family);
    } else if (
      part.type === 'function_call' ||
      part.type === 'function_call_output'
    ) {
      total += textHundredths(
        String(part.name ?? '') + String(part.arguments ?? part.output ?? ''),
        family
      );
    } else if (part.content !== undefined) {
      total += contentHundredths(part.content, family);
    }
  }
  return total;
}

/**
 * Estimated input tokens of a generation request body: Chat Completions
 * (`messages`), Anthropic Messages (`system`, `messages`), Responses
 * (`instructions`, `input`) and legacy Completions (`prompt`), plus tool
 * definitions as JSON and the framing tokens per message. Undefined for a
 * body with none of these.
 */
export function estimateRequestTokens(
  body: any,
  family: TokenFamily = 'cl100k'
): number | undefined {
  if (!body || typeof body !== 'object') return undefined;
  let total = 0;
  let messages = 0;
  let seen = false;
  const add = (v: unknown) => {
    if (v === undefined || v === null) return;
    seen = true;
    total += contentHundredths(v, family);
  };
  add(body.system);
  add(body.instructions);
  if (typeof body.prompt === 'string') add(body.prompt);
  else if (Array.isArray(body.prompt)) add(body.prompt.join('\n'));
  const list = Array.isArray(body.messages)
    ? body.messages
    : Array.isArray(body.input)
      ? body.input
      : undefined;
  if (typeof body.input === 'string') {
    seen = true;
    messages++;
    total += textHundredths(body.input, family);
  }
  for (const m of list ?? []) {
    if (!m || typeof m !== 'object') continue;
    seen = true;
    messages++;
    if (m.content !== undefined) add(m.content);
    else add([m]);
    for (const call of Array.isArray(m.tool_calls) ? m.tool_calls : []) {
      const fn = call?.function;
      if (fn && typeof fn === 'object')
        total += textHundredths(
          String(fn.name ?? '') + String(fn.arguments ?? ''),
          family
        );
    }
  }
  if (Array.isArray(body.tools) && body.tools.length)
    total += textHundredths(json(body.tools), family);
  if (!seen) return undefined;
  return (
    round(total) + MESSAGE_FRAMING_TOKENS * messages + REPLY_FRAMING_TOKENS
  );
}
