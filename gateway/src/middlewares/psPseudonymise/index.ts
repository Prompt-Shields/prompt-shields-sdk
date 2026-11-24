/**
 * PS fork (#350): reversible pseudonymisation of model calls (opt-in).
 *
 * When enabled (central `pseudonymisation` section, else PS_PSEUDONYMISE;
 * see ./config.ts), each Chat Completions, Anthropic Messages and OpenAI
 * Responses request has the PII values the detector finds replaced by
 * typed placeholders (`⟦EMAIL_1⟧`, see ./core.ts) before anything below
 * this middleware sees it, and the answer has them put back, streamed or
 * not, on its way out. The mapping lives in this request's memory only and
 * is dropped when the response (or stream) ends.
 *
 * Mounted after the router and budgets (src/index.ts): the data policy
 * classifies and routes on the original prompt, never on placeholders, so a
 * pseudonymised call is never routed below the sensitivity of what the
 * caller wrote. Mounted before telemetry, OpenTelemetry, guardrail hooks
 * and the response cache, so none of them (nor the provider) receives an
 * original value: they see the placeholders in the request and in the
 * answer, and cached answers hold placeholders only.
 *
 * Disabled (the default) it calls next() and does nothing else: requests
 * and responses are byte-identical to a gateway without it. Enabled, a
 * request in which nothing is found is also passed through untouched.
 */
import { Context, Next } from 'hono';
import { isMcpProxyPath } from '../../mcp/paths';
import { centralPseudonymisation } from '../policyPull/state';
import { PseudonymisationSettings, pseudonymisationFromEnv } from './config';
import { UNRESTORED_RESPONSE_KEY } from './contextKeys';
import { Pseudonymisation, Surface } from './core';
import { restoreSSE } from './stream';

/** Response header: how many distinct values were replaced (a count only). */
export const PSEUDONYMISED_HEADER = 'x-ps-pseudonymised';

/** The request shape at `path`, if the gateway can pseudonymise it. */
export function surfaceOf(method: string, path: string): Surface | undefined {
  if (method !== 'POST') return undefined;
  switch (path.replace(/\/+$/, '')) {
    case '/v1/chat/completions':
      return 'chat';
    case '/v1/messages':
    case '/v1/messages/count_tokens':
      return 'messages';
    case '/v1/responses':
    case '/v1/responses/input_tokens':
      return 'responses';
    default:
      return undefined;
  }
}

/**
 * True for requests that carry no prompt: reads, deletes, CORS preflight,
 * cancelling a response, feedback (which goes to the collector, not a
 * provider) and MCP proxy traffic (not a model call, #63). Realtime is a
 * GET upgrade that carries prompts, so it is not one.
 */
function carriesNoPrompt(method: string, path: string): boolean {
  if (isMcpProxyPath(path) || path === '/v1/feedback') return true;
  if (/^\/v1\/responses\/[^/]+\/cancel\/?$/.test(path)) return true;
  if (method === 'GET') return !path.startsWith('/v1/realtime');
  return method === 'HEAD' || method === 'OPTIONS' || method === 'DELETE';
}

function refuse(c: Context, message: string): Response {
  return c.json(
    {
      error: {
        message: `Pseudonymisation is enabled: ${message}`,
        type: 'invalid_request_error',
        param: null,
        code: 'pseudonymisation_unsupported',
      },
    },
    400
  );
}

const isObj = (v: unknown): v is Record<string, any> =>
  !!v && typeof v === 'object' && !Array.isArray(v);

// Body fields that carry the prompt (what pseudonymiseBody walks).
const PROMPT_FIELDS = [
  'messages',
  'system',
  'prediction',
  'input',
  'instructions',
  'prompt',
];

/**
 * True when the request's gateway config (x-portkey-config) sets prompt
 * fields through `override_params` at any target: those are merged into
 * the upstream body below this middleware, so they would reach the
 * provider unreplaced.
 */
export function configOverridesPrompt(header: string | undefined): boolean {
  if (!header) return false;
  let config: unknown;
  try {
    config = JSON.parse(header);
  } catch {
    return false; // not an inline config: the handler rejects or ignores it
  }
  const stack: [unknown, number][] = [[config, 0]];
  while (stack.length) {
    const [node, depth] = stack.pop() as [unknown, number];
    if (depth > 32 || !node || typeof node !== 'object') continue;
    if (!Array.isArray(node)) {
      for (const key of ['override_params', 'overrideParams']) {
        const op = (node as Record<string, any>)[key];
        if (isObj(op) && PROMPT_FIELDS.some((f) => f in op)) return true;
      }
    }
    for (const v of Object.values(node)) stack.push([v, depth + 1]);
  }
  return false;
}

/** Settings in force for this request. */
export type SettingsProvider = () => PseudonymisationSettings | undefined;

/**
 * `provide` overrides where settings come from (tests); by default the
 * central section, else PS_PSEUDONYMISE* from `env`, parsed once here so
 * an invalid local setting fails the start.
 */
export function psPseudonymise(
  provide?: SettingsProvider,
  env: Record<string, string | undefined> = typeof process !== 'undefined'
    ? process.env
    : {}
) {
  const local = pseudonymisationFromEnv(env);
  const settingsOf: SettingsProvider =
    provide ?? (() => centralPseudonymisation() ?? local);

  return async (c: Context, next: Next) => {
    const settings = settingsOf();
    if (!settings?.enabled) return next();

    const method = c.req.method;
    const path = c.req.path;
    const surface = surfaceOf(method, path);
    const strict = settings.onUnsupported === 'refuse';
    if (!surface) {
      if (strict && !carriesNoPrompt(method, path))
        return refuse(c, `${method} ${path} cannot be pseudonymised`);
      return next();
    }
    if (strict && configOverridesPrompt(c.req.header('x-portkey-config')))
      return refuse(
        c,
        'override_params in the gateway config cannot set prompt fields (messages, system, input, ...)'
      );
    const contentType = (c.req.header('content-type') || '').toLowerCase();
    if (!contentType.includes('application/json')) {
      if (strict) return refuse(c, 'the request body must be JSON');
      return next();
    }
    let body: unknown;
    try {
      body = await c.req.json();
    } catch {
      // Not JSON: the handler rejects it, and it reaches no provider.
      return next();
    }
    if (!isObj(body)) return next();
    if (
      surface === 'responses' &&
      strict &&
      (body.previous_response_id != null || body.conversation != null)
    )
      return refuse(
        c,
        'previous_response_id and conversation (provider-side state) are not supported; send the conversation in input'
      );

    const pseudo = new Pseudonymisation(settings.categories);
    let replaced: unknown;
    try {
      replaced = pseudo.pseudonymiseBody(surface, body);
    } catch (err) {
      // Fail closed: never forward a body that was meant to be replaced.
      pseudo.clear();
      console.warn(
        '[PS Pseudonymise] refusing the call:',
        (err as Error)?.name
      );
      return refuse(c, 'the request could not be pseudonymised');
    }
    if (!pseudo.active) return next();

    const entities = Object.values(pseudo.stats().entities).reduce(
      (a, b) => a + b,
      0
    );
    const serialized = JSON.stringify(replaced);
    // Every later body read (json(), text(), the raw request) gets the
    // pseudonymised body.
    (c.req as any).bodyCache = { text: Promise.resolve(serialized) };
    const headers = new Headers(c.req.raw.headers);
    headers.delete('content-length');
    c.req.raw = new Request(c.req.raw.url, {
      method,
      headers,
      body: serialized,
      signal: c.req.raw.signal,
    });

    let streaming = false;
    try {
      await next();
      const res = c.res;
      const resType = (res.headers.get('content-type') || '').toLowerCase();
      const encoding = (res.headers.get('content-encoding') || 'identity')
        .trim()
        .toLowerCase();
      if (res.status < 200 || res.status >= 300 || encoding !== 'identity')
        return;
      let out: Response | undefined;
      if (resType.includes('text/event-stream') && res.body) {
        streaming = true;
        out = new Response(
          restoreSSE(res.body, pseudo, () => pseudo.clear()),
          res
        );
      } else if (resType.includes('application/json')) {
        let parsed: unknown;
        let text: string;
        try {
          text = await res.clone().text();
          parsed = JSON.parse(text);
        } catch {
          return;
        }
        // For layers above this one that must not see original values
        // (the /log/stream tail): the answer as the provider sent it.
        c.set(UNRESTORED_RESPONSE_KEY as any, text);
        pseudo.restoreResponse(parsed);
        out = new Response(JSON.stringify(parsed), res);
      }
      if (out) {
        out.headers.delete('content-length');
        out.headers.set(PSEUDONYMISED_HEADER, String(entities));
        // Hono copies the old response's headers onto a new one (and with
        // them a stale content-length) unless it is unset first.
        c.res = undefined as any;
        c.res = out;
      }
    } finally {
      if (!streaming) pseudo.clear();
    }
  };
}
