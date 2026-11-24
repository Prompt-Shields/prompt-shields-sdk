/**
 * PS fork (#350): JSON read and written the way the SDK's pseudonymiser
 * does it (Python's `json.loads` / `json.dumps(..., ensure_ascii=False)`),
 * for tool-call `arguments` strings. Rewritten arguments are then
 * byte-for-byte what the SDK sends for the same input, so mixed SDK and
 * gateway deployments agree (schemas/pseudonymisation/vectors.v1.json).
 *
 * Differences from JSON.parse / JSON.stringify that matter here: object key
 * order is kept exactly (JS objects put integer-like keys first), integers
 * keep every digit (no 2^53 limit), floats print as Python's `repr`
 * (`1.0`, `1e+16`), and `", "` / `": "` separators. Linear time; nesting
 * deeper than MAX_DEPTH is refused (the caller then treats the arguments
 * as plain text, as the SDK does for anything that does not parse).
 */

/** A JSON number, kept as written. */
export class PyNumber {
  constructor(
    readonly lexeme: string,
    readonly isInt: boolean
  ) {}
}

export type PyValue =
  | string
  | PyNumber
  | boolean
  | null
  | PyValue[]
  | Map<string, PyValue>;

const MAX_DEPTH = 256;

export class PyJsonError extends Error {}

const WS = new Set([' ', '\t', '\n', '\r']);
const NUMBER_RE = /-?(?:0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?/y;
const KEYWORDS: [string, () => PyValue][] = [
  ['true', () => true],
  ['false', () => false],
  ['null', () => null],
  ['NaN', () => new PyNumber('NaN', false)],
  ['Infinity', () => new PyNumber('Infinity', false)],
  ['-Infinity', () => new PyNumber('-Infinity', false)],
];

type Frame =
  | { kind: 'array'; value: PyValue[] }
  | { kind: 'object'; value: Map<string, PyValue>; key?: string };

/** Python's json.loads, strict mode (NaN and Infinity accepted, as Python does). */
export function pyLoads(text: string): PyValue {
  let pos = 0;
  const skip = () => {
    while (pos < text.length && WS.has(text[pos])) pos++;
  };
  const fail = (): never => {
    throw new PyJsonError(`invalid JSON at ${pos}`);
  };

  const parseString = (): string => {
    pos++; // the opening quote
    let out = '';
    let chunkStart = pos;
    while (true) {
      if (pos >= text.length) fail();
      const ch = text.charCodeAt(pos);
      if (ch === 0x22) {
        out += text.slice(chunkStart, pos);
        pos++;
        return out;
      }
      if (ch < 0x20) fail(); // strict: no raw control characters
      if (ch !== 0x5c) {
        pos++;
        continue;
      }
      out += text.slice(chunkStart, pos);
      const esc = text[pos + 1];
      pos += 2;
      const simple: Record<string, string> = {
        '"': '"',
        '\\': '\\',
        '/': '/',
        b: '\b',
        f: '\f',
        n: '\n',
        r: '\r',
        t: '\t',
      };
      if (esc in simple) out += simple[esc];
      else if (esc === 'u') {
        const hex = text.slice(pos, pos + 4);
        if (!/^[0-9a-fA-F]{4}$/.test(hex)) fail();
        out += String.fromCharCode(parseInt(hex, 16));
        pos += 4;
      } else fail();
      chunkStart = pos;
    }
  };

  const parseScalar = (): PyValue => {
    if (text[pos] === '"') return parseString();
    for (const [word, make] of KEYWORDS) {
      if (text.startsWith(word, pos)) {
        pos += word.length;
        return make();
      }
    }
    NUMBER_RE.lastIndex = pos;
    const m = NUMBER_RE.exec(text);
    if (!m) return fail();
    pos += m[0].length;
    return new PyNumber(m[0], !m[1] && !m[2]);
  };

  // An explicit stack instead of recursion: deep nesting is refused, never
  // a stack overflow.
  const stack: Frame[] = [];
  let result: PyValue | undefined;
  let done = false;

  const emit = (value: PyValue) => {
    const top = stack[stack.length - 1];
    if (!top) {
      result = value;
      done = true;
    } else if (top.kind === 'array') top.value.push(value);
    else {
      // Python dicts keep a repeated key at its first position, last value.
      top.value.set(top.key as string, value);
      top.key = undefined;
    }
  };

  // After a value inside a container: a comma, or the container's end
  // (closing as many containers as end here).
  const afterValue = () => {
    while (!done) {
      skip();
      const top = stack[stack.length - 1];
      if (text[pos] === ',') {
        pos++;
        if (top.kind === 'object') {
          skip();
          if (text[pos] !== '"') fail();
        }
        return;
      }
      if (text[pos] !== (top.kind === 'array' ? ']' : '}')) fail();
      pos++;
      stack.pop();
      emit(top.value);
    }
  };

  skip();
  while (!done) {
    const top = stack[stack.length - 1];
    if (top?.kind === 'object' && top.key === undefined) {
      skip();
      if (text[pos] !== '"') fail();
      top.key = parseString();
      skip();
      if (text[pos] !== ':') fail();
      pos++;
    }
    skip();
    const ch = text[pos];
    if (ch === '[' || ch === '{') {
      if (stack.length >= MAX_DEPTH) fail();
      pos++;
      skip();
      const close = ch === '[' ? ']' : '}';
      if (text[pos] === close) {
        pos++;
        emit(ch === '[' ? [] : new Map());
        afterValueIfNested();
        continue;
      }
      stack.push(
        ch === '['
          ? { kind: 'array', value: [] }
          : { kind: 'object', value: new Map() }
      );
      continue;
    }
    emit(parseScalar());
    afterValueIfNested();
  }
  skip();
  if (pos !== text.length) fail();
  return result as PyValue;

  function afterValueIfNested() {
    if (!done) afterValue();
  }
}

/** Python's repr of a float, from the JSON number lexeme it was read from. */
function pyFloat(lexeme: string): string {
  if (lexeme === 'NaN' || lexeme === 'Infinity' || lexeme === '-Infinity')
    return lexeme;
  const x = Number(lexeme);
  if (!Number.isFinite(x)) return x > 0 ? 'Infinity' : '-Infinity';
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  // toExponential() with no argument: the shortest digits that round-trip,
  // which is what Python's repr uses too.
  const [mant, expPart] = x.toExponential().split('e');
  const exp = Number(expPart);
  const sign = mant.startsWith('-') ? '-' : '';
  const digits = mant.replace('-', '').replace('.', '');
  if (exp < -4 || exp >= 16) {
    const m = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const e = Math.abs(exp).toString().padStart(2, '0');
    return `${sign}${m}e${exp < 0 ? '-' : '+'}${e}`;
  }
  if (exp < 0) return `${sign}0.${'0'.repeat(-exp - 1)}${digits}`;
  const intLen = exp + 1;
  if (digits.length <= intLen)
    return `${sign}${digits}${'0'.repeat(intLen - digits.length)}.0`;
  return `${sign}${digits.slice(0, intLen)}.${digits.slice(intLen)}`;
}

/** Python's str(int(lexeme)) ("-0" is "0"). */
export function pyIntText(lexeme: string): string {
  return lexeme === '-0' ? '0' : lexeme;
}

/** Python's json.dumps(value, ensure_ascii=False). */
export function pyDumps(value: PyValue): string {
  const out: string[] = [];
  const write = (v: PyValue) => {
    if (typeof v === 'string') out.push(JSON.stringify(v));
    else if (v instanceof PyNumber)
      out.push(v.isInt ? pyIntText(v.lexeme) : pyFloat(v.lexeme));
    else if (v === true) out.push('true');
    else if (v === false) out.push('false');
    else if (v === null) out.push('null');
    else if (Array.isArray(v)) {
      out.push('[');
      v.forEach((item, i) => {
        if (i) out.push(', ');
        write(item);
      });
      out.push(']');
    } else {
      out.push('{');
      let first = true;
      for (const [k, item] of v) {
        if (!first) out.push(', ');
        first = false;
        out.push(JSON.stringify(k), ': ');
        write(item);
      }
      out.push('}');
    }
  };
  write(value);
  return out.join('');
}
