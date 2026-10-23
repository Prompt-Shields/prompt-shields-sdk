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
  ['email', re(String.raw`[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}`)],
  [
    'phone',
    re(String.raw`(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3,4}[-.\s]?\d{4}`),
  ],
  ['ssn', re(String.raw`\b\d{3}-\d{2}-\d{4}\b`)],
  ['credit_card', re(String.raw`\b(?:\d[ -]*?){13,19}\b`)],
  ['ip_address', re(String.raw`\b(?:\d{1,3}\.){3}\d{1,3}\b`)],
  ['iban', re(String.raw`\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b`)],
];

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
  for (const [name, pattern] of PATTERNS) {
    if (pattern.test(text)) found.add(name);
  }
  if (containsNordicNationalId(text)) found.add('national_id');
  const lower = text.toLowerCase();
  for (const [category, keywords] of KEYWORD_CATEGORIES) {
    if (keywords.some((kw) => lower.includes(kw))) found.add(category);
  }
  return [...found].sort();
}

/**
 * Detection over every message's text: string content and the `text` of
 * content parts, joined with spaces as the SDK's `scan_messages` does.
 */
export function scanMessages(messages: any[]): string[] {
  const texts: string[] = [];
  for (const m of messages || []) {
    const content = m?.content;
    if (typeof content === 'string') texts.push(content);
    else if (Array.isArray(content)) {
      for (const part of content) {
        if (typeof part?.text === 'string') texts.push(part.text);
      }
    }
  }
  return detectPiiCategories(texts.join(' '));
}
