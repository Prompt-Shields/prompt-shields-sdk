/**
 * PII category detection for sensitivity escalation (#18).
 *
 * A port of the SDK's pattern detector (packages/sdk/prompt_shields/pii.py
 * and national_ids.py), so the gateway escalates on the same categories the
 * SDK reports as `detected_pii_types`. Pattern based, no network, presence
 * only: matched text is never returned or stored. Best effort, so it can
 * only raise a call's sensitivity level, never lower it.
 */

// Python's \w and \b are Unicode-aware; [\p{L}\p{N}_] and the boundary B
// below keep the matches identical to the SDK's (\d stays ASCII here).
const W = '[\\p{L}\\p{N}_]';
const B = `(?:(?<=${W})(?!${W})|(?<!${W})(?=${W}))`;
const L = `(?<![\\p{L}\\p{N}_+-])`;
const R = `(?!${W})(?![-.]\\d)`;

const re = (source: string) => new RegExp(source.replace(/\\b/g, B), 'u');

const PATTERNS: [string, RegExp][] = [
  // The SDK's pattern, anchored at the start of a local-part run so a long
  // run without a match is scanned once, not once per start position
  // (quadratic: 100 KB of letters took ~8 s). Same yes/no answer: any match
  // inside a run implies one from the run's start.
  [
    'email',
    re(
      String.raw`(?<![a-zA-Z0-9._%+-])[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`
    ),
  ],
  [
    'phone',
    // Not inside a longer digit run (card numbers, IBANs); letters next to
    // it are fine ("Tel:5551234567", "555-123-4567x89").
    re(
      String.raw`(?<!\p{Nd})(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\p{Nd})`
    ),
  ],
  ['ssn', re(String.raw`\b\d{3}-\d{2}-\d{4}\b`)],
  ['ip_address', re(String.raw`\b(?:\d{1,3}\.){3}\d{1,3}\b`)],
];

// Shape regex + validator: the category is found when any match validates.

// Card numbers: 13-19 digits with optional separators, Luhn checked. The
// shape is \b(?:\d[ -]*?){13,19}\b, retried from every digit group
// ("Ref 12 4111 1111 1111 1111"); hasCard evaluates it in one scan over runs
// of 13+ digits joined by spaces and dashes.
// The lookahead checks for 13 digits; the rest takes whole groups at a time.
const CARD_RUN_RE = /\d(?=(?:[ -]*\d){12})\d*(?:[ -]+\d+)*/g;
const WORD_BEFORE = new RegExp(`${W}$`, 'u');
const WORD_AFTER = new RegExp(`^${W}`, 'u');
// IBAN, compact or in groups of four, mod-97 checked.
const IBAN_RE = new RegExp(
  String.raw`\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b`.replace(
    /\\b/g,
    B
  ),
  'gu'
);
// Grouped phone numbers the US-shaped pattern misses: international
// ("+46 70 174 06 12") and national with a trunk 0 ("070-174 06 12").
const INTL_PHONE_RE = new RegExp(
  String.raw`(?<![\p{L}\p{N}_+])\+\d[\d ()./-]{6,20}\d(?!${W})`,
  'gu'
);
const TRUNK_PHONE_RE = new RegExp(
  String.raw`(?<![\p{L}\p{N}_+.,/-])\(?0\d[\d ()./-]{6,16}\d(?!${W})`,
  'gu'
);

/**
 * True when the retry loop over \b(?:\d[ -]*?){13,19}\b finds a
 * Luhn-valid match. That loop tries the pattern from every digit group start
 * (a group: a run of digits; a start: one after a non-word character). From
 * a start the pattern takes whole groups of its run (digits joined by spaces
 * and dashes) until it has 13 digits, then stops at the end of that group
 * (the lazy separator ends the match there); it fails when that is more than
 * 19 digits, or the run ends there before a word character. Here each start
 * is checked once, when the group that completes its window is read, instead
 * of by a fresh regex match, so runs like "1-1-1-..." are not rescanned per
 * group.
 */
function hasCard(text: string): boolean {
  return !cardWindows(text).next().done;
}

/**
 * The [start, end) offsets of every Luhn-valid window hasCard accepts (see
 * there), in order; windows of one run may overlap.
 */
function* cardWindows(text: string): Generator<[number, number]> {
  // A private copy: a caller may stop iterating part way.
  const runRe = new RegExp(CARD_RUN_RE.source, 'g');
  let m: RegExpExecArray | null;
  while ((m = runRe.exec(text)) !== null) {
    const run = m[0];
    const end = m.index + run.length;
    // A window may end at the run's last group only before a non-word
    // character.
    const lastOk = !WORD_AFTER.test(text.slice(end, end + 2));
    // Group g is run[from[g], to[g]), with cum[g] digits before it.
    const from: number[] = [];
    const to: number[] = [];
    const cum = [0];
    // The next start to check; the run's first group is a start only after
    // a non-word character.
    let i = WORD_BEFORE.test(text.slice(Math.max(0, m.index - 2), m.index))
      ? 1
      : 0;
    let p = 0;
    while (p < run.length) {
      from.push(p);
      while (p < run.length && isDigit(run.charCodeAt(p))) p++;
      to.push(p);
      while (p < run.length && !isDigit(run.charCodeAt(p))) p++;
      const e = to.length;
      cum.push(cum[e - 1] + to[e - 1] - from[e - 1]);
      // Starts whose window (the fewest groups with 13+ digits) ends here.
      for (; i < e && cum[e] - cum[i] >= 13; i++) {
        if (
          cum[e] - cum[i] <= 19 &&
          (p < run.length || lastOk) &&
          luhnRangeOk(run, from[i], to[e - 1])
        ) {
          yield [m.index + from[i], m.index + to[e - 1]];
        }
      }
    }
  }
}

function isDigit(code: number): boolean {
  return code >= 48 && code <= 57;
}

// luhnOk over the digits of s[from, to), without building the string.
function luhnRangeOk(s: string, from: number, to: number): boolean {
  let total = 0;
  let double = false;
  for (let p = to - 1; p >= from; p--) {
    const code = s.charCodeAt(p);
    if (!isDigit(code)) continue;
    let d = code - 48;
    if (double) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    total += d;
    double = !double;
  }
  return total % 10 === 0;
}

// `rest` with the IBAN digits of `c` (A=10 ... Z=35) appended, mod 97.
function mod97(rest: number, c: string): number {
  const v = parseInt(c, 36);
  return (rest * (v < 10 ? 10 : 100) + v) % 97;
}

function maskIban(match: string): string {
  const n = ibanValidLength(match);
  return n < 0 ? match : ' '.repeat(n) + match.slice(n);
}

/** Length of the checksum-valid prefix of an IBAN_RE match, else -1. */
function ibanValidLength(match: string): number {
  // A following word or digit group ("... 7466 BIC", "... 7466 4111 1111")
  // can join the last group: drop trailing space-separated groups until the
  // checksum holds, and blank only that prefix so what follows is still
  // scanned (a card number right after an IBAN must not be hidden). The
  // checksum: 15-34 characters, and the digits of compact[4:] + compact[:4]
  // are 1 mod 97. The remainder after the first four characters is folded
  // once and shared by every prefix.
  const groups = match.split(' ');
  const compact = groups.join('');
  // body[i]: remainder of compact.slice(4, 4 + i).
  const body = [0];
  for (let k = 4; k < compact.length; k++)
    body.push(mod97(body[body.length - 1], compact[k]));
  let nCompact = compact.length;
  let nSpaced = match.length;
  for (let g = groups.length - 1; g >= 0; g--) {
    if (nCompact >= 15 && nCompact <= 34) {
      let rest = body[nCompact - 4];
      for (let k = 0; k < 4; k++) rest = mod97(rest, compact[k]);
      if (rest === 1) return nSpaced;
    }
    nCompact -= groups[g].length;
    nSpaced -= groups[g].length + 1;
  }
  return -1;
}

function phoneShapeOk(s: string, min: number, max: number): boolean {
  const digits = (s.match(/\d/g) || []).length;
  if (digits < min || digits > max) return false;
  const seps = s.replace(/[()]/g, '').match(/[ ./-]+/g) || [];
  // One separator between groups, the same one after the first group.
  if (seps.some((sep) => sep.length > 1)) return false;
  return new Set(seps.slice(1)).size <= 1;
}

const VALIDATED: [string, RegExp, (m: RegExpExecArray) => boolean][] = [
  ['phone', INTL_PHONE_RE, (m) => phoneShapeOk(m[0], 8, 15)],
  [
    'phone',
    TRUNK_PHONE_RE,
    // A bare digit run is left to the pattern above.
    (m) => /[ ./-]/.test(m[0]) && phoneShapeOk(m[0], 9, 12),
  ],
];

function anyValid(
  pattern: RegExp,
  text: string,
  validate: (m: RegExpExecArray) => boolean
): boolean {
  pattern.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = pattern.exec(text)) !== null) {
    if (validate(m)) {
      pattern.lastIndex = 0;
      return true;
    }
    if (m[0] === '') pattern.lastIndex++;
  }
  return false;
}

const KEYWORD_CATEGORIES: [string, string[]][] = [
  [
    'health_data',
    [
      'diagnosis',
      'prescription',
      'medication',
      'patient id',
      'medical record',
      'icd-10',
      'blood pressure',
      'lab result',
    ],
  ],
  [
    'financial_data',
    [
      'account number',
      'routing number',
      'iban',
      'swift code',
      'tax id',
      'vat number',
    ],
  ],
];

// --- Nordic national IDs (checksum validated) -------------------------------

function validDate(year: number, month: number, day: number): boolean {
  if (month < 1 || month > 12 || day < 1) return false;
  return day <= new Date(Date.UTC(year, month, 0)).getUTCDate();
}

function luhnOk(digits: string): boolean {
  let total = 0;
  const rev = digits.split('').reverse();
  rev.forEach((ch, i) => {
    let d = Number(ch);
    if (i % 2 === 1) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    total += d;
  });
  return total % 10 === 0;
}

// Sweden: [CC]YYMMDD[-+]NNNC, Luhn over YYMMDDNNNC; day + 60 for
// samordningsnummer. A bare 10-digit run is not accepted.
const SE_RE = new RegExp(
  `${L}((?:18|19|20)?)(\\d{2})(\\d{2})(\\d{2})([-+]?)(\\d{3})(\\d)${R}`,
  'gu'
);
function validSe(m: RegExpExecArray): boolean {
  const [, century, yy, mm, dd, sep, serial, check] = m;
  if (!century && !sep) return false;
  let day = Number(dd);
  if (day > 60) day -= 60;
  const year = century ? Number(century + yy) : 2000 + Number(yy);
  if (!validDate(year, Number(mm), day)) return false;
  return luhnOk(yy + mm + dd + serial + check);
}

// Norway: DDMMYYIIIKK with two weighted mod-11 check digits.
const NO_RE = new RegExp(`${L}(\\d{6})[ ]?(\\d{5})${R}`, 'gu');
const NO_W1 = [3, 7, 6, 1, 8, 9, 4, 5, 2];
const NO_W2 = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2];

function mod11Check(digits: string, weights: number[]): number | null {
  let sum = 0;
  weights.forEach((w, i) => (sum += Number(digits[i]) * w));
  const rest = sum % 11;
  const k = rest === 0 ? 0 : 11 - rest;
  return k === 10 ? null : k;
}

function noYear(yy: number, individual: number): number | null {
  if (individual <= 499) return 1900 + yy;
  if (individual <= 749 && yy >= 54) return 1800 + yy;
  if (individual >= 500 && yy <= 39) return 2000 + yy;
  if (individual >= 900 && yy >= 40) return 1900 + yy;
  return null;
}

function validNo(m: RegExpExecArray): boolean {
  const digits = m[1] + m[2];
  let day = Number(digits.slice(0, 2));
  let month = Number(digits.slice(2, 4));
  const yy = Number(digits.slice(4, 6));
  if (day > 40) day -= 40;
  if (month > 80) month -= 80;
  else if (month > 40) month -= 40;
  const year = noYear(yy, Number(digits.slice(6, 9)));
  if (year === null || !validDate(year, month, day)) return false;
  const k1 = mod11Check(digits.slice(0, 9), NO_W1);
  if (k1 === null || k1 !== Number(digits[9])) return false;
  const k2 = mod11Check(digits.slice(0, 10), NO_W2);
  return k2 !== null && k2 === Number(digits[10]);
}

// Denmark: DDMMYY-SSSS; mod-11 only required without the hyphen.
const DK_RE = new RegExp(`${L}(\\d{2})(\\d{2})(\\d{2})(-?)(\\d{4})${R}`, 'gu');
const DK_W = [4, 3, 2, 7, 6, 5, 4, 3, 2, 1];

function dkYear(yy: number, d7: number): number {
  if (d7 <= 3) return 1900 + yy;
  if (d7 === 4 || d7 === 9) return yy <= 36 ? 2000 + yy : 1900 + yy;
  return yy <= 57 ? 2000 + yy : 1800 + yy;
}

function validDk(m: RegExpExecArray): boolean {
  const [, dd, mm, yy, sep, serial] = m;
  const year = dkYear(Number(yy), Number(serial[0]));
  if (!validDate(year, Number(mm), Number(dd))) return false;
  if (sep) return true;
  const digits = dd + mm + yy + serial;
  let sum = 0;
  DK_W.forEach((w, i) => (sum += Number(digits[i]) * w));
  return sum % 11 === 0;
}

// Finland: DDMMYYCZZZQ, century marker and mod-31 check character.
const FI_RE = new RegExp(
  `${L}(\\d{2})(\\d{2})(\\d{2})([-+ABCDEFUVWXY])(\\d{3})([0-9A-Y])${R}`,
  'giu'
);
const FI_CHECK = '0123456789ABCDEFHJKLMNPRSTUVWXY';
const FI_CENTURY: Record<string, number> = { '+': 1800, '-': 1900, A: 2000 };
for (const c of 'UVWXY') FI_CENTURY[c] = 1900;
for (const c of 'BCDEF') FI_CENTURY[c] = 2000;

function validFi(m: RegExpExecArray): boolean {
  const [, dd, mm, yy, marker, individual, check] = m;
  const year = FI_CENTURY[marker.toUpperCase()] + Number(yy);
  if (!validDate(year, Number(mm), Number(dd))) return false;
  if (Number(individual) < 2) return false;
  return (
    FI_CHECK[Number(dd + mm + yy + individual) % 31] === check.toUpperCase()
  );
}

const DETECTORS: [RegExp, (m: RegExpExecArray) => boolean][] = [
  [SE_RE, validSe],
  [NO_RE, validNo],
  [DK_RE, validDk],
  [FI_RE, validFi],
];

/** True when `text` contains a checksum-valid Nordic national ID. */
export function containsNordicNationalId(text: string): boolean {
  for (const [pattern, validate] of DETECTORS) {
    pattern.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = pattern.exec(text)) !== null) {
      if (validate(m)) {
        pattern.lastIndex = 0;
        return true;
      }
      if (m[0] === '') pattern.lastIndex++;
    }
  }
  return false;
}

/** PII category names found in `text`, sorted. Empty when none. */
export function detectPiiCategories(text: string | undefined | null): string[] {
  if (!text) return [];
  const found = new Set<string>();
  // IBANs first: a checksum-valid IBAN is blanked out before the other
  // patterns run, so its digits never also count as a card or phone number.
  const original = text;
  const masked = text.replace(IBAN_RE, maskIban);
  if (masked !== original) found.add('iban');
  if (hasCard(masked)) found.add('credit_card');
  for (const [name, pattern] of PATTERNS) {
    if (pattern.test(masked)) found.add(name);
  }
  for (const [name, pattern, validate] of VALIDATED) {
    if (!found.has(name) && anyValid(pattern, masked, validate))
      found.add(name);
  }
  if (containsNordicNationalId(masked)) found.add('national_id');
  const lower = original.toLowerCase();
  for (const [category, keywords] of KEYWORD_CATEGORIES) {
    if (keywords.some((kw) => lower.includes(kw))) found.add(category);
  }
  return [...found].sort();
}

// --- Span finding, for redaction (#337) -------------------------------------

/** A PII match: text[start, end) is of `category` (a detectPiiCategories name). */
export interface PiiSpan {
  start: number;
  end: number;
  category: string;
}

// Where spans overlap, the merged span takes the first of these categories.
const SPAN_PRIORITY = [
  'iban',
  'credit_card',
  'national_id',
  'ssn',
  'email',
  'ip_address',
  'phone',
];

const SPAN_PATTERNS: [string, RegExp][] = PATTERNS.map(([name, pattern]) => [
  name,
  new RegExp(pattern.source, 'gu'),
]);

function* validMatches(
  pattern: RegExp,
  text: string,
  validate: (m: RegExpExecArray) => boolean
): Generator<RegExpExecArray> {
  // A private copy: the shared global regexes keep lastIndex.
  const global = new RegExp(pattern.source, pattern.flags);
  let m: RegExpExecArray | null;
  while ((m = global.exec(text)) !== null) {
    if (validate(m)) yield m;
    if (m[0] === '') global.lastIndex++;
  }
}

/**
 * Where detectPiiCategories' pattern categories are in `text`: the same
 * patterns and validators, each match reported instead of presence only.
 * Overlapping matches are merged (see SPAN_PRIORITY). The keyword
 * categories (health_data, financial_data) mark context, not a value, and
 * have no spans. Spans are sorted and disjoint.
 */
export function findPiiSpans(text: string | undefined | null): PiiSpan[] {
  if (!text) return [];
  const raw: PiiSpan[] = [];
  const add = (start: number, end: number, category: string) => {
    if (end > start) raw.push({ start, end, category });
  };
  // As in detectPiiCategories: valid IBANs are blanked (same length, so
  // offsets hold) before the other patterns run.
  const masked = text.replace(IBAN_RE, (match: string, offset: number) => {
    const n = ibanValidLength(match);
    if (n < 0) return match;
    add(offset, offset + n, 'iban');
    return ' '.repeat(n) + match.slice(n);
  });
  for (const [start, end] of cardWindows(masked))
    add(start, end, 'credit_card');
  for (const [name, pattern] of SPAN_PATTERNS)
    for (const m of validMatches(pattern, masked, () => true))
      add(m.index, m.index + m[0].length, name);
  for (const [name, pattern, validate] of VALIDATED)
    for (const m of validMatches(pattern, masked, validate))
      add(m.index, m.index + m[0].length, name);
  for (const [pattern, validate] of DETECTORS)
    for (const m of validMatches(pattern, masked, validate))
      add(m.index, m.index + m[0].length, 'national_id');

  raw.sort((a, b) => a.start - b.start || b.end - a.end);
  const merged: PiiSpan[] = [];
  for (const span of raw) {
    const last = merged[merged.length - 1];
    if (last && span.start < last.end) {
      last.end = Math.max(last.end, span.end);
      if (
        SPAN_PRIORITY.indexOf(span.category) <
        SPAN_PRIORITY.indexOf(last.category)
      )
        last.category = span.category;
    } else {
      merged.push({ ...span });
    }
  }
  return merged;
}

/** Placeholder for a redacted span: `[EMAIL]`, `[CREDIT_CARD]`, ... */
export function piiPlaceholder(category: string): string {
  return `[${category.toUpperCase()}]`;
}

/** `text` with every findPiiSpans span replaced by its placeholder. */
export function redactPii(text: string): string {
  const spans = findPiiSpans(text);
  if (!spans.length) return text;
  let out = '';
  let pos = 0;
  for (const { start, end, category } of spans) {
    out += text.slice(pos, start) + piiPlaceholder(category);
    pos = end;
  }
  return out + text.slice(pos);
}

// Keys whose string values are request text (messages, Responses input and
// instructions, Anthropic system, completions prompt, embeddings input,
// rerank query/documents, speech input, tool arguments and outputs).
const TEXT_KEYS = new Set([
  'content',
  'text',
  'input',
  'prompt',
  'instructions',
  'system',
  'arguments',
  'output',
  'query',
  'documents',
]);
// Tool-call arguments given as objects (Anthropic tool_use `input`, parsed
// `arguments`): every string below them is request text.
const ARG_KEYS = new Set(['input', 'arguments', 'args']);
// Binary payloads, URLs and schema/config blocks: never request text, and
// base64 would only add false positives.
const SKIP_KEYS = new Set([
  'model',
  'image_url',
  'file',
  'file_data',
  'file_id',
  'b64_json',
  'data',
  'audio',
  'input_audio',
  'source',
  'image',
  'mask',
  'url',
  'tools',
  'functions',
  'tool_choice',
  'response_format',
  'metadata',
]);

/**
 * Request text of any JSON request shape the gateway serves (chat, Responses,
 * Anthropic Messages, completions, embeddings, rerank, images, speech and
 * proxied bodies): see TEXT_KEYS. Strings are joined with spaces.
 */
export function requestText(body: unknown): string {
  const out: string[] = [];
  const walk = (node: unknown, key: string, all: boolean, depth: number) => {
    if (depth > 64 || node == null) return;
    if (typeof node === 'string') {
      if (all || TEXT_KEYS.has(key)) out.push(node);
    } else if (Array.isArray(node)) {
      for (const item of node) walk(item, key, all, depth + 1);
    } else if (typeof node === 'object') {
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
        if (SKIP_KEYS.has(k)) continue;
        const args =
          ARG_KEYS.has(k) && typeof v === 'object' && !Array.isArray(v);
        walk(v, k, all || args, depth + 1);
      }
    }
  };
  walk(body, '', false, 0);
  return out.join(' ');
}

/** PII categories in any JSON request body (see requestText). */
export function scanRequestBody(body: unknown): string[] {
  return detectPiiCategories(requestText(body));
}
