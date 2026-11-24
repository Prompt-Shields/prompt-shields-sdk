/**
 * PS fork (#349): guardrails enforced by the gateway from the central
 * policy's `guardrails` section.
 *
 * They run on every model call whatever the caller sends: they are not
 * part of the Portkey config (`x-portkey-config`, `input_guardrails`), so a
 * caller cannot remove or weaken them; guardrails the caller adds still
 * run on top. Which rules apply: the tenant's, plus those of the team and
 * project the call names in X-PS-Team / X-PS-Project (policyPull/state.ts
 * centralGuardrails), for the call's models (config.ts rulesFor).
 *
 *   input   JSON request text (messages, prompts, tool arguments, ...):
 *           `deny` refuses the call with 446 before any upstream request;
 *           `redact` replaces the finding with a placeholder (`[EMAIL]`)
 *           in the body every later middleware and the provider see;
 *           `log` records it.
 *   output  the response's model text, JSON or streamed (./stream.ts):
 *           `deny` replaces a JSON response with a 446 error (a stream
 *           ends with an SSE error event), `redact` rewrites it, `log`
 *           records it.
 *
 * Findings are recorded as security events (rule id, category, count and
 * the policy version, never content) and the input's PII categories go to
 * telemetry as `detected_pii_types`.
 *
 * With no guardrails in the central policy this middleware does nothing:
 * it does not read the body or touch the response.
 *
 * A request body is read as JSON whatever its Content-Type says (the
 * handlers parse it that way too, so a missing or wrong header must not
 * skip the input scan). GET responses on /v1 (a stored Responses object,
 * stored chat completions, conversation items) get the output stage.
 *
 * The same output rules also scrub what the gateway keeps or exports of
 * a response (GUARDRAIL_SCRUB_KEY): the cache write-back, OTel content
 * capture and the /log/stream entries, which see the provider's response
 * before this middleware rewrites it.
 *
 * Not inspected (passed through): non-JSON request bodies (multipart
 * uploads, audio), responses that are neither JSON nor SSE (audio,
 * images, JSONL), error responses, JSON responses over MAX_SCAN_BYTES,
 * realtime WebSockets, and the MCP proxy, files, batches (batch input
 * files and outputs) and fine-tuning routes.
 */
import { Context, Next } from 'hono';
import { mapRequestText } from '../router/pii';
import { centralGuardrails, hasCentralGuardrails } from '../policyPull/state';
import {
  PolicyGuardrailFinding,
  recordPolicyGuardrailFindings,
} from '../psSecurityEvents';
import { configModels, GuardrailsSection, rulesFor } from './config';
import {
  evaluate,
  mapOutputText,
  prepareStage,
  redact,
  StageRules,
  Tally,
} from './scan';
import { guardEventStream } from './stream';
import { GUARDRAIL_SCRUB_KEY, OutputScrub } from './scrub';

export { GUARDRAIL_SCRUB_KEY } from './scrub';

/** Context key: the PII categories input guardrails found (sorted). */
export const GUARDRAIL_PII_KEY = 'psGuardrailPii';
/** JSON responses larger than this are passed through unscanned. */
export const MAX_SCAN_BYTES = 16 * 1024 * 1024;
/** As Portkey's guardrail denials. */
export const GUARDRAIL_DENIED_STATUS = 446;

const SKIPPED_PREFIXES = [
  '/v1/mcp/',
  '/v1/files',
  '/v1/batches',
  '/v1/fine_tuning',
  '/v1/realtime',
  '/v1/feedback',
];

const inScope = (c: Context) =>
  (c.req.method === 'POST' || c.req.method === 'GET') &&
  c.req.path.startsWith('/v1/') &&
  !SKIPPED_PREFIXES.some((p) => c.req.path.startsWith(p));

/** First non-whitespace byte: a JSON object or array starts the body. */
function looksLikeJson(buf: ArrayBuffer): boolean {
  const bytes = new Uint8Array(buf);
  // Whitespace and a UTF-8 byte order mark may come first.
  const skip = new Set([0x20, 0x09, 0x0a, 0x0d, 0xef, 0xbb, 0xbf]);
  for (let i = 0; i < bytes.length && i < 1024; i++)
    if (!skip.has(bytes[i])) return bytes[i] === 0x7b || bytes[i] === 0x5b;
  return false;
}

/**
 * The request body as JSON, whatever the Content-Type: the handlers call
 * c.req.json() without checking it, so a call without the header (or with
 * a wrong one) must be scanned like any other. Read as bytes, so a body
 * that is not JSON (multipart, audio) reaches the handler unchanged.
 */
async function readJsonBody(c: Context): Promise<any> {
  const type = (c.req.header('content-type') || '').toLowerCase();
  try {
    if (type.startsWith('application/json')) return await c.req.json();
    const buf = await c.req.arrayBuffer();
    if (!buf.byteLength || !looksLikeJson(buf)) return undefined;
    return JSON.parse(new TextDecoder().decode(buf));
  } catch {
    return undefined; // not JSON: the handler rejects it
  }
}

export function deniedBody(stage: 'input' | 'output') {
  return {
    error: {
      message:
        stage === 'input'
          ? 'The request was blocked by a guardrail policy'
          : 'The response was blocked by a guardrail policy',
      type: 'guardrail_violation',
      code: 'ps_guardrail_denied',
    },
  };
}

function findingsOf(tally: Tally): PolicyGuardrailFinding[] {
  return [...tally.entries.values()].map((e) => ({
    stage: e.stage,
    ruleId: e.rule.id,
    check: e.rule.check,
    category: e.category,
    action: e.action,
    count: e.count,
    policyVersion: e.rule.policyVersion,
  }));
}

/** Scans one text with `stage`, tallying hits; returns the redacted text. */
function scanText(text: string, stage: StageRules, tally: Tally): string {
  const hits = evaluate(text, stage);
  if (!hits.length) return text;
  for (const h of hits) tally.add(stage.stage, h);
  return redact(text, hits);
}

const replaceResponse = (
  c: Context,
  res: Response,
  body: BodyInit | null,
  rewritten: boolean
) => {
  c.res = new Response(body, {
    status: res.status,
    statusText: res.statusText,
    headers: res.headers,
  });
  if (rewritten) {
    c.res.headers.delete('content-length');
    c.res.headers.delete('content-encoding');
  }
};

/**
 * `guardrails` returns the guardrails for a call's team and project
 * (default: the central policy's); `active` says whether any exist at all,
 * checked first so a gateway without guardrails does no work per call.
 */
export function psGuardrails(
  guardrails: (
    team?: string,
    project?: string
  ) => GuardrailsSection | undefined = centralGuardrails,
  active: () => boolean = hasCentralGuardrails
) {
  return async (c: Context, next: Next) => {
    if (!active() || !inScope(c)) return next();
    const set = guardrails(
      c.req.header('x-ps-team') || undefined,
      c.req.header('x-ps-project') || undefined
    );
    if (!set || !set.rules.length) return next();

    const tally = new Tally();
    let body: any;
    let model: string | undefined;
    if (c.req.method === 'POST') {
      body = await readJsonBody(c);
      if (body && typeof body.model === 'string') model = body.model;
    }
    const models = [model, ...configModels(c.req.header('x-portkey-config'))];
    const record = () =>
      recordPolicyGuardrailFindings(c, findingsOf(tally), {
        provider: c.req.header('x-portkey-provider') || undefined,
        model,
      });

    // Input.
    const input = prepareStage('input', rulesFor(set.rules, 'input', models));
    if (input && body && typeof body === 'object') {
      const changed = mapRequestText(body, (t) => scanText(t, input, tally));
      if (tally.denied) {
        record();
        return c.json(deniedBody('input'), GUARDRAIL_DENIED_STATUS as any);
      }
      // json() re-reads the cache on every call: point it at the redacted body.
      if (changed) (c.req as any).bodyCache = { json: Promise.resolve(body) };
    }
    const pii = tally.piiCategories('input');
    if (pii.length) c.set(GUARDRAIL_PII_KEY as any, pii);

    const outputStage = () =>
      prepareStage(
        'output',
        rulesFor(set.rules, 'output', [
          ...models,
          c.get('psRoute')?.servedModel,
        ])
      );
    if (set.rules.some((r) => r.stages.includes('output'))) {
      const scrub: OutputScrub = (obj) => {
        const stage = outputStage();
        if (!stage || !obj || typeof obj !== 'object') return false;
        const scratch = new Tally();
        mapOutputText(obj, (_p, t) => scanText(t, stage, scratch));
        return !!scratch.denied;
      };
      c.set(GUARDRAIL_SCRUB_KEY as any, scrub);
    }

    await next();

    // Output.
    const output = outputStage();
    const res = c.res;
    if (!output || !res || !res.ok || !res.body) return record();
    const type = res.headers.get('content-type') || '';

    if (type.includes('text/event-stream')) {
      const guarded = guardEventStream(res.body, {
        stage: output,
        windowChars: set.streamWindowChars,
        tally,
        onEnd: record,
      });
      replaceResponse(c, res, guarded, true);
      return;
    }

    if (!type.includes('json')) return record();
    const declared = Number(res.headers.get('content-length'));
    if (Number.isFinite(declared) && declared > MAX_SCAN_BYTES) return record();
    let text: string;
    try {
      text = await res.text();
    } catch {
      return record();
    }
    let obj: any;
    try {
      obj = text.length <= MAX_SCAN_BYTES ? JSON.parse(text) : undefined;
    } catch {
      obj = undefined;
    }
    if (!obj || typeof obj !== 'object') {
      replaceResponse(c, res, text, false);
      return record();
    }
    let changed = false;
    mapOutputText(obj, (_p, t) => {
      const next = scanText(t, output, tally);
      if (next !== t) changed = true;
      return next;
    });
    record();
    if (tally.denied) {
      c.res = new Response(JSON.stringify(deniedBody('output')), {
        status: GUARDRAIL_DENIED_STATUS,
        headers: { 'content-type': 'application/json' },
      });
      c.res.headers.delete('content-length');
      c.res.headers.delete('content-encoding');
      return;
    }
    replaceResponse(c, res, changed ? JSON.stringify(obj) : text, changed);
  };
}
