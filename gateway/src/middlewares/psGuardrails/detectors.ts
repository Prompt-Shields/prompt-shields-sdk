/**
 * PS fork (#349): the guardrails' native detectors.
 *
 * - `pii`: the router's detector (../router/pii.ts): findPiiSpans for the
 *   value categories, detectKeywordCategories for the keyword categories
 *   (health_data, financial_data), which mark context and have no span.
 * - `secrets`: provider API keys, cloud and SaaS tokens, private keys and
 *   JWTs, by their published prefixes and shapes.
 * - `injection`: well-known prompt-injection phrasings and chat-template
 *   control tokens. A marker list, not a classifier: it catches the
 *   copy-pasted attacks, not a determined adversary.
 *
 * Every pattern runs in linear time: no nested or overlapping
 * quantifiers, every repetition either bounded or over a class the next
 * token cannot match, so a match attempt never backtracks more than a
 * bounded amount (guardrails.test.ts checks 1 MB adversarial inputs).
 * Matched text is never logged or returned beyond its offsets.
 */
import { detectKeywordCategories, findPiiSpans } from '../router/pii';
import { GuardrailCheck, INJECTION_CATEGORY } from './config';

export interface Finding {
  check: GuardrailCheck;
  category: string;
  /** text[start, end); absent for context categories, which have no span. */
  start?: number;
  end?: number;
}

// Not part of a longer token on the left.
const L = '(?<![A-Za-z0-9_-])';

const SECRET_PATTERNS: [string, RegExp][] = [
  ['anthropic_key', new RegExp(`${L}sk-ant-[A-Za-z0-9_-]{20,}`, 'g')],
  [
    'openai_key',
    new RegExp(
      `${L}sk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}`,
      'g'
    ),
  ],
  [
    'aws_access_key',
    new RegExp(`${L}(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Z])`, 'g'),
  ],
  [
    'github_token',
    new RegExp(
      `${L}(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})`,
      'g'
    ),
  ],
  ['slack_token', new RegExp(`${L}xox[abposr]-[A-Za-z0-9-]{10,}`, 'g')],
  ['google_api_key', new RegExp(`${L}AIza[0-9A-Za-z_-]{35}`, 'g')],
  [
    'stripe_key',
    new RegExp(`${L}(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}`, 'g'),
  ],
  [
    'private_key',
    // The header and as much of the base64 body as follows (bounded, and
    // the body class excludes '-', so it stops at the END line).
    /-----BEGIN (?:[A-Z]{1,20} ){0,2}PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,8192}(?:-----END (?:[A-Z]{1,20} ){0,2}PRIVATE KEY-----)?/g,
  ],
  [
    'jwt',
    new RegExp(
      `${L}eyJ[A-Za-z0-9_-]{8,}\\.eyJ[A-Za-z0-9_-]{8,}\\.[A-Za-z0-9_-]{8,}`,
      'g'
    ),
  ],
];

const S = '[ \\t\\r\\n]{1,5}';
const INJECTION_PATTERNS: RegExp[] = [
  new RegExp(
    `\\b(?:ignore|disregard|forget|override|bypass)${S}(?:(?:all|any|the|your|my|these|those|previous|prior|above|earlier|preceding|system|original)${S}){0,4}(?:instructions|directives|rules|prompts?|guidelines|guardrails)\\b`,
    'gi'
  ),
  new RegExp(
    `\\byou${S}are${S}now${S}(?:in${S})?(?:dan|developer${S}mode|jailbroken|unrestricted|unfiltered)\\b`,
    'gi'
  ),
  new RegExp(
    `\\b(?:reveal|print|show|repeat|output|leak)${S}(?:me${S})?(?:your|the)${S}(?:system|hidden|initial|secret)${S}(?:prompt|instructions|message)\\b`,
    'gi'
  ),
  /<\|(?:im_start\|>[ \t]{0,5}system|system\|>|endoftext\|>)/gi,
];

function spansOf(
  pattern: RegExp,
  text: string,
  check: GuardrailCheck,
  category: string,
  out: Finding[]
) {
  // A private copy: global regexes keep lastIndex.
  const re = new RegExp(pattern.source, pattern.flags);
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m[0] === '') {
      re.lastIndex++;
      continue;
    }
    out.push({ check, category, start: m.index, end: m.index + m[0].length });
  }
}

/** Secret spans in `text`, in pattern order. */
export function findSecrets(text: string): Finding[] {
  const out: Finding[] = [];
  for (const [category, pattern] of SECRET_PATTERNS)
    spansOf(pattern, text, 'secrets', category, out);
  return out;
}

/** Prompt-injection marker spans in `text`. */
export function findInjection(text: string): Finding[] {
  const out: Finding[] = [];
  for (const pattern of INJECTION_PATTERNS)
    spansOf(pattern, text, 'injection', INJECTION_CATEGORY, out);
  return out;
}

/** Which checks to run and whether keyword categories are wanted. */
export interface DetectorSelection {
  pii: boolean;
  piiContext: boolean;
  secrets: boolean;
  injection: boolean;
}

/** Every finding of the selected checks in `text`. */
export function detect(text: string, sel: DetectorSelection): Finding[] {
  if (!text) return [];
  const out: Finding[] = [];
  if (sel.pii)
    for (const s of findPiiSpans(text))
      out.push({
        check: 'pii',
        category: s.category,
        start: s.start,
        end: s.end,
      });
  if (sel.piiContext)
    for (const category of detectKeywordCategories(text))
      out.push({ check: 'pii', category });
  if (sel.secrets) out.push(...findSecrets(text));
  if (sel.injection) out.push(...findInjection(text));
  return out;
}
