/**
 * PS fork (#350): reversible pseudonymisation, a port of the SDK's
 * packages/sdk/prompt_shields/pseudonymise.py.
 *
 * Each distinct PII value the detector finds (findEntitySpans: email,
 * phone, US SSN, card, IP address, IBAN, Nordic national ID) is replaced by
 * a typed placeholder such as `⟦EMAIL_1⟧`; `restore` puts the values back
 * into the answer. Placeholders are minted exactly as the SDK mints them
 * (same spans, same numbering, same walk order, same tool-argument JSON),
 * so a call pseudonymised by the SDK and one pseudonymised by the gateway
 * carry the same placeholders (schemas/pseudonymisation/vectors.v1.json).
 *
 * The mapping lives only in the Pseudonymisation object, for one request:
 * it is never logged, put on telemetry, cached or sent, `toJSON` /
 * `inspect` show counts only, and `clear()` drops it. No key material is
 * involved because nothing is ever persisted.
 *
 * Restoration only replaces placeholders this object minted. Placeholders
 * the model invents, ones already in the prompt, and look-alikes
 * (`[EMAIL_1]`) are left as they are. Case and spaces inside the brackets
 * are tolerated.
 *
 * Beyond the SDK: OpenAI Responses requests (`instructions`, `input` items,
 * function-call arguments and outputs) and responses (JSON and stream
 * events). The SDK does not pseudonymise Responses calls, so those have no
 * cross-language vectors.
 */
import { findEntitySpans } from '../router/pii';
import { PyJsonError, PyNumber, PyValue, pyDumps, pyLoads } from './pyjson';

export const OPEN = '⟦'; // ⟦
export const CLOSE = '⟧'; // ⟧

// A placeholder, tolerating case and spaces inside the brackets. Linear:
// the lazy name cannot take digits, so each `⟦` is tried along one run of
// [A-Za-z_\s\d] at most (ReDoS-tested in core.test.ts).
const BODY = String.raw`\s*([A-Za-z][A-Za-z_]*?)_(\d{1,6})\s*`;
const TOKEN = new RegExp(OPEN + BODY + CLOSE, 'g');
// Inside JSON (tool-call arguments) the brackets may also be \u escapes.
const JSON_OPEN = String.raw`(?:${OPEN}|\\u27[eE]6)`;
const JSON_CLOSE = String.raw`(?:${CLOSE}|\\u27[eE]7)`;
const TOKEN_JSON = new RegExp(JSON_OPEN + BODY + JSON_CLOSE, 'g');
// What a placeholder looks like before its closing bracket has arrived.
const PARTIAL = new RegExp(
  String.raw`^(?:${OPEN}\s*(?:[A-Za-z][A-Za-z_]*\d*\s*)?)$`
);
const ESC_PREFIX = String.raw`\\(?:u(?:2(?:7(?:[eE])?)?)?)?`;
const PARTIAL_JSON = new RegExp(
  String.raw`^(?:${ESC_PREFIX}|${JSON_OPEN}\s*(?:[A-Za-z][A-Za-z_]*\d*\s*(?:${ESC_PREFIX})?)?)$`
);
// Longest tail a stream holds back while waiting for a closing bracket.
const MAX_PENDING = 40;
// Nesting deeper than this is not walked (a request body is not that deep).
const MAX_DEPTH = 64;

type Json = any;
type TextFn = (s: string) => string;

const isObj = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

/** Counts only, never values. */
export interface PseudonymisationStats {
  /** Distinct values replaced, per category. */
  entities: Record<string, number>;
  /** Placeholders put back. */
  restored: number;
  /** Placeholder-shaped tokens in the answer that were left as they are. */
  unknown: number;
}

/** The request shapes the gateway pseudonymises. */
export type Surface = 'chat' | 'messages' | 'responses';

export class Pseudonymisation {
  #byToken = new Map<string, string>();
  #byValue = new Map<string, string>();
  #counts: Record<string, number> = {};
  #next = new Map<string, number>();
  #reserved = new Set<string>();
  #restored = 0;
  #unknown = 0;
  #wanted?: Set<string>;

  /** `categories` limits which categories are replaced (default: all). */
  constructor(categories?: Iterable<string>) {
    this.#wanted = categories ? new Set(categories) : undefined;
  }

  // --- privacy guards ------------------------------------------------------

  toString(): string {
    return `<Pseudonymisation entities=${this.#byToken.size}>`;
  }

  toJSON(): { entities: number } {
    return { entities: this.#byToken.size };
  }

  [Symbol.for('nodejs.util.inspect.custom')](): string {
    return this.toString();
  }

  /** Drop the mapping. Later restores leave every placeholder as it is. */
  clear(): void {
    this.#byToken.clear();
    this.#byValue.clear();
  }

  /** True when at least one value was replaced (and not cleared). */
  get active(): boolean {
    return this.#byToken.size > 0;
  }

  stats(): PseudonymisationStats {
    return {
      entities: { ...this.#counts },
      restored: this.#restored,
      unknown: this.#unknown,
    };
  }

  // --- pseudonymising --------------------------------------------------------

  /** Placeholder-shaped text in `text` is never minted. */
  reserve = (text: string): string => {
    for (const m of text.matchAll(TOKEN_JSON))
      this.#reserved.add(`${m[1].toUpperCase()}_${parseInt(m[2], 10)}`);
    return text;
  };

  #token(value: string, category: string): string {
    const known = this.#byValue.get(value);
    if (known !== undefined) return known;
    const kind = category.toUpperCase();
    let n = this.#next.get(kind) ?? 1;
    while (this.#reserved.has(`${kind}_${n}`)) n++;
    this.#next.set(kind, n + 1);
    const token = `${OPEN}${kind}_${n}${CLOSE}`;
    this.#byValue.set(value, token);
    this.#byToken.set(`${kind}_${n}`, value);
    this.#counts[category] = (this.#counts[category] ?? 0) + 1;
    return token;
  }

  /** `text` with every detected value replaced by its placeholder. */
  replace = (text: string): string => {
    let out = '';
    let last = 0;
    for (const { start, end, category } of findEntitySpans(text)) {
      if (this.#wanted && !this.#wanted.has(category)) continue;
      out +=
        text.slice(last, start) + this.#token(text.slice(start, end), category);
      last = end;
    }
    return last ? out + text.slice(last) : text;
  };

  /**
   * The SDK's Pseudonymisation(messages, system=, prediction=): `system`
   * first, then `messages`, then `prediction`, each a new value (the input
   * is not changed). Placeholder-shaped text anywhere in them (and in
   * signed thinking blocks, which are never changed) is reserved first.
   */
  pseudonymiseChat(
    messages: Json[] | undefined,
    system?: Json,
    prediction?: Json
  ): { messages: Json[]; system: Json; prediction: Json } {
    const list = Array.isArray(messages) ? messages : [];
    for (const m of list) {
      walkMessage(m, this.reserve);
      for (const signed of signedTexts(m)) this.reserve(signed);
    }
    walkContent(system, this.reserve);
    walkPrediction(prediction, this.reserve);
    return {
      system: walkContent(system, this.replace),
      messages: list.map((m) => walkMessage(m, this.replace)),
      prediction: walkPrediction(prediction, this.replace),
    };
  }

  /**
   * A request body for `surface`, pseudonymised (a new object; the input
   * is not changed). Chat Completions: `messages` and `prediction`.
   * Anthropic Messages: `system` and `messages`. Responses: `instructions`,
   * then `input`, then `prompt.variables`.
   */
  pseudonymiseBody(surface: Surface, body: Json): Json {
    if (!isObj(body)) return body;
    if (surface === 'responses') {
      walkResponsesRequest(body, this.reserve);
      return walkResponsesRequest(body, this.replace);
    }
    const anthropic = surface === 'messages';
    const out = this.pseudonymiseChat(
      body.messages,
      anthropic ? body.system : undefined,
      anthropic ? undefined : body.prediction
    );
    const next: Json = { ...body };
    if (Array.isArray(body.messages)) next.messages = out.messages;
    if (anthropic && body.system !== undefined) next.system = out.system;
    if (!anthropic && body.prediction !== undefined)
      next.prediction = out.prediction;
    return next;
  }

  // --- restoring -------------------------------------------------------------

  /**
   * `text` with this request's placeholders replaced by their values.
   * `jsonEscape` escapes each value for use inside a JSON string (tool-call
   * arguments) and also matches `⟦` escaped brackets.
   */
  restore(text: string, jsonEscape = false): string {
    if (!text || (!text.includes(OPEN) && !jsonEscape)) return text;
    return text.replace(
      jsonEscape ? TOKEN_JSON : TOKEN,
      (whole: string, kind: string, n: string) => {
        const value = this.#byToken.get(
          `${kind.toUpperCase()}_${parseInt(n, 10)}`
        );
        if (value === undefined) {
          this.#unknown++;
          return whole;
        }
        this.#restored++;
        return jsonEscape ? JSON.stringify(value).slice(1, -1) : value;
      }
    );
  }

  /** A restorer for one text stream fed in pieces. */
  streamRestorer(jsonEscape = false): StreamRestorer {
    return new StreamRestorer(this, jsonEscape);
  }

  /**
   * A whole (not streamed) response, restored in place and returned. Chat
   * Completions: each choice's `content`, `refusal` and tool-call
   * `arguments`. Anthropic Messages: `text` blocks and `tool_use` input.
   * Responses: output messages' text and refusal parts, function-call
   * arguments and custom tool input, and `output_text`. Thinking and
   * reasoning are left as they are.
   */
  restoreResponse(response: Json): Json {
    if (!isObj(response)) return response;
    if (Array.isArray(response.choices)) {
      for (const choice of response.choices) {
        const message = isObj(choice) ? choice.message : undefined;
        if (!isObj(message)) continue;
        for (const name of ['content', 'refusal'])
          if (typeof message[name] === 'string')
            message[name] = this.restore(message[name]);
        const fns = (
          Array.isArray(message.tool_calls) ? message.tool_calls : []
        ).map((tc: Json) => (isObj(tc) ? tc.function : undefined));
        fns.push(message.function_call);
        for (const fn of fns)
          if (isObj(fn) && typeof fn.arguments === 'string')
            fn.arguments = this.restore(fn.arguments, true);
      }
      return response;
    }
    if (response.object === 'response' || Array.isArray(response.output)) {
      this.restoreResponsesObject(response);
      return response;
    }
    if (Array.isArray(response.content)) {
      for (const block of response.content) {
        if (!isObj(block)) continue;
        if (block.type === 'text' && typeof block.text === 'string')
          block.text = this.restore(block.text);
        else if (block.type === 'tool_use' && isObj(block.input))
          block.input = jsonValue(block.input, (s) => this.restore(s));
      }
    }
    return response;
  }

  /** A Responses API `response` object, restored in place. */
  restoreResponsesObject(response: Json): void {
    if (!isObj(response)) return;
    if (Array.isArray(response.output))
      for (const item of response.output) this.restoreResponsesItem(item);
    if (typeof response.output_text === 'string')
      response.output_text = this.restore(response.output_text);
  }

  /** One Responses output item (or content part), restored in place. */
  restoreResponsesItem(item: Json): void {
    if (!isObj(item)) return;
    if (Array.isArray(item.content))
      for (const part of item.content) this.restoreResponsesItem(part);
    if (
      (item.type === 'output_text' || item.type === 'text') &&
      typeof item.text === 'string'
    )
      item.text = this.restore(item.text);
    if (item.type === 'refusal' && typeof item.refusal === 'string')
      item.refusal = this.restore(item.refusal);
    if (item.type === 'function_call' && typeof item.arguments === 'string')
      item.arguments = this.restore(item.arguments, true);
    if (item.type === 'custom_tool_call' && typeof item.input === 'string')
      item.input = this.restore(item.input);
  }
}

/**
 * Restores one text stream fed in pieces (one per content field). `feed`
 * returns what can be emitted now; a tail that could still become a
 * placeholder (up to 40 characters after `⟦`) is held back until it is
 * complete or clearly is not one. `flush` returns the rest at the end.
 */
export class StreamRestorer {
  #pending = '';
  constructor(
    private readonly owner: Pseudonymisation,
    private readonly json: boolean
  ) {}

  get pending(): boolean {
    return this.#pending.length > 0;
  }

  feed(text: string | undefined | null): string {
    if (!text) return '';
    let data = this.#pending + text;
    this.#pending = '';
    const partial = this.json ? PARTIAL_JSON : PARTIAL;
    const starts = [data.lastIndexOf(OPEN)];
    if (this.json)
      starts.push(
        data.lastIndexOf('\\u27e6'),
        data.lastIndexOf('\\u27E6'),
        data.lastIndexOf('\\')
      );
    // The earliest start whose tail could still become a placeholder.
    const cuts = starts
      .filter((c) => c !== -1 && data.length - c <= MAX_PENDING)
      .sort((a, b) => a - b);
    for (const cut of cuts) {
      if (partial.test(data.slice(cut))) {
        this.#pending = data.slice(cut);
        data = data.slice(0, cut);
        break;
      }
    }
    return this.owner.restore(data, this.json);
  }

  flush(): string {
    const data = this.#pending;
    this.#pending = '';
    return this.owner.restore(data, this.json);
  }
}

// --- request walkers (the SDK's _walk_* functions) ---------------------------

/**
 * `text` applied to every string, keys included, in a JSON value. Integers
 * are scanned as their digits, as they appear on the wire; one that changes
 * becomes the replaced string. Floats and booleans are kept.
 */
export function jsonValue(value: Json, text: TextFn, depth = 0): Json {
  if (typeof value === 'string') return text(value);
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) return value;
    const digits = BigInt(value).toString();
    const replaced = text(digits);
    return replaced === digits ? value : replaced;
  }
  if (depth >= MAX_DEPTH) return value;
  if (Array.isArray(value))
    return value.map((v) => jsonValue(v, text, depth + 1));
  if (isObj(value)) {
    const out: Record<string, Json> = {};
    for (const [k, v] of Object.entries(value))
      out[text(k)] = jsonValue(v, text, depth + 1);
    return out;
  }
  return value;
}

// jsonValue over the exact parse (pyjson.ts), for arguments strings.
function pyValue(value: PyValue, text: TextFn): PyValue {
  if (typeof value === 'string') return text(value);
  if (value instanceof PyNumber) {
    if (!value.isInt) return value;
    const digits = value.lexeme === '-0' ? '0' : value.lexeme;
    const replaced = text(digits);
    return replaced === digits ? value : replaced;
  }
  if (Array.isArray(value)) return value.map((v) => pyValue(v, text));
  if (value instanceof Map) {
    const out = new Map<string, PyValue>();
    for (const [k, v] of value) out.set(text(k), pyValue(v, text));
    return out;
  }
  return value;
}

/**
 * Tool-call arguments: JSON string values (and keys) when they parse,
 * re-serialised as the SDK's json.dumps does; else the raw text.
 */
export function walkArguments(args: string, text: TextFn): string {
  let parsed: PyValue;
  try {
    parsed = pyLoads(args);
  } catch (err) {
    if (err instanceof PyJsonError) return text(args);
    throw err;
  }
  return pyDumps(pyValue(parsed, text));
}

/**
 * `text` applied to message content: a string or a list of parts. Text and
 * refusal parts, Anthropic tool_result (string or part-list content),
 * tool_use (strings in input), document (text or content source, title,
 * context) and search_result (title, content) parts. Images, audio, files
 * and other parts are kept as they are.
 */
export function walkContent(content: Json, text: TextFn, depth = 0): Json {
  if (typeof content === 'string') return text(content);
  if (!Array.isArray(content) || depth >= MAX_DEPTH) return content;
  return content.map((part) => {
    if (!isObj(part)) return part;
    let p = part;
    if (typeof p.text === 'string') p = { ...p, text: text(p.text) };
    if (p.type === 'refusal' && typeof p.refusal === 'string')
      p = { ...p, refusal: text(p.refusal) };
    if (
      (p.type === 'tool_result' || p.type === 'search_result') &&
      'content' in p
    )
      p = { ...p, content: walkContent(p.content, text, depth + 1) };
    if (p.type === 'document' || p.type === 'search_result')
      for (const name of ['title', 'context'])
        if (typeof p[name] === 'string') p = { ...p, [name]: text(p[name]) };
    if (p.type === 'document' && isObj(p.source))
      p = { ...p, source: walkDocumentSource(p.source, text, depth + 1) };
    if (p.type === 'tool_use' && isObj(p.input))
      p = { ...p, input: jsonValue(p.input, text) };
    return p;
  });
}

function walkDocumentSource(source: Json, text: TextFn, depth: number): Json {
  if (source.type === 'text' && typeof source.data === 'string')
    return { ...source, data: text(source.data) };
  if (source.type === 'content' && 'content' in source)
    return { ...source, content: walkContent(source.content, text, depth) };
  return source;
}

/** The text of Anthropic thinking / redacted_thinking blocks (read only). */
function* signedTexts(message: Json): Generator<string> {
  if (!isObj(message) || !Array.isArray(message.content)) return;
  for (const part of message.content) {
    if (!isObj(part)) continue;
    const value =
      part.type === 'thinking'
        ? part.thinking
        : part.type === 'redacted_thinking'
          ? part.data
          : undefined;
    if (typeof value === 'string') yield value;
  }
}

function walkPrediction(prediction: Json, text: TextFn): Json {
  if (isObj(prediction) && 'content' in prediction)
    return { ...prediction, content: walkContent(prediction.content, text) };
  return prediction;
}

/**
 * A copy of one chat message with `text` applied to its content, refusal
 * and assistant tool-call / legacy function_call arguments.
 */
export function walkMessage(message: Json, text: TextFn): Json {
  if (!isObj(message)) return message;
  const out: Json = { ...message };
  if ('content' in out) out.content = walkContent(out.content, text);
  if (typeof out.refusal === 'string') out.refusal = text(out.refusal);
  const fc = out.function_call;
  if (isObj(fc) && typeof fc.arguments === 'string')
    out.function_call = { ...fc, arguments: walkArguments(fc.arguments, text) };
  if (Array.isArray(out.tool_calls))
    out.tool_calls = out.tool_calls.map((tc: Json) => {
      const fn = isObj(tc) ? tc.function : undefined;
      if (isObj(fn) && typeof fn.arguments === 'string')
        return {
          ...tc,
          function: { ...fn, arguments: walkArguments(fn.arguments, text) },
        };
      return tc;
    });
  return out;
}

/**
 * A Responses API request with `text` applied: `instructions`, then
 * `input` (a string, or items: messages with string or part-list content,
 * function_call arguments, function_call_output and custom tool output,
 * custom_tool_call input), then reusable-prompt `prompt.variables`.
 */
export function walkResponsesRequest(body: Json, text: TextFn): Json {
  const out: Json = { ...body };
  if (typeof body.instructions === 'string')
    out.instructions = text(body.instructions);
  if (typeof body.input === 'string') out.input = text(body.input);
  else if (Array.isArray(body.input))
    out.input = body.input.map((item: Json) => {
      if (!isObj(item)) return item;
      let it = walkMessage(item, text);
      if (it.type === 'function_call' && typeof it.arguments === 'string')
        it = { ...it, arguments: walkArguments(it.arguments, text) };
      if (
        (it.type === 'function_call_output' ||
          it.type === 'custom_tool_call_output') &&
        'output' in it
      )
        it = { ...it, output: walkContent(it.output, text) };
      if (it.type === 'custom_tool_call' && typeof it.input === 'string')
        it = { ...it, input: text(it.input) };
      return it;
    });
  const vars = isObj(body.prompt) ? body.prompt.variables : undefined;
  if (isObj(vars)) {
    const replaced: Json = {};
    for (const [k, v] of Object.entries(vars))
      replaced[k] = typeof v === 'string' ? text(v) : walkContent([v], text)[0];
    out.prompt = { ...body.prompt, variables: replaced };
  }
  return out;
}
