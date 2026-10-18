/**
 * PS fork (security): guardrails on the generic proxy routes.
 *
 * Upstream Portkey (fork base, OSS `main` and 2.0.0) never runs hooks on
 * `fn === 'proxy'`: `shouldSkipHook` only accepts the unified endpoint types.
 * So a caller whose config has input/output guardrails could reach the same
 * model API through `/v1/proxy/*` (or the `/v1/*` catch-all) and skip them.
 *
 * Here, for a target with hooks or guardrails, a proxied request that is
 * recognisably a Chat Completions, Completions, Messages, Responses or
 * Embeddings call (path suffix and JSON body shape) runs the target's hooks
 * with that endpoint type; any other proxied request is refused with a 400
 * before the upstream call. Targets without hooks are proxied unchanged.
 */
import { Context } from 'hono';
import { CONTENT_TYPES } from '../globals';
import { endpointStrings } from '../providers/types';

export type ProxyHooksDecision =
  /** Run the target's hooks as this endpoint type. */
  | { kind: 'hooks'; fn: endpointStrings }
  /** No model input or output to inspect (model listing). */
  | { kind: 'noContent' }
  /** Not recognisable: guardrails cannot be applied. */
  | { kind: 'unsupported'; reason: string };

const HOOK_FIELDS = [
  'beforeRequestHooks',
  'afterRequestHooks',
  'inputGuardrails',
  'outputGuardrails',
  'defaultInputGuardrails',
  'defaultOutputGuardrails',
  // Before camel-casing (configs built outside constructConfigFromRequestHeaders).
  'before_request_hooks',
  'after_request_hooks',
  'input_guardrails',
  'output_guardrails',
  'default_input_guardrails',
  'default_output_guardrails',
  'inputMutators',
  'outputMutators',
  'input_mutators',
  'output_mutators',
];

const MAX_CONFIG_DEPTH = 32;

/**
 * Whether any target in the config tree (root, fallback, load-balance or
 * conditional children) declares hooks or guardrails. Hooks are inherited
 * downwards, so a declaration anywhere means some leaf runs them.
 */
export function configHasHooks(config: any, depth = 0): boolean {
  if (!config || typeof config !== 'object' || depth > MAX_CONFIG_DEPTH) {
    return false;
  }
  for (const key of HOOK_FIELDS) {
    const value = config[key];
    if (Array.isArray(value) ? value.length > 0 : Boolean(value)) return true;
  }
  return Array.isArray(config.targets)
    ? config.targets.some((t: any) => configHasHooks(t, depth + 1))
    : false;
}

/**
 * The request path with the proxy route prefix removed. Shared with
 * `ProviderContext.getProxyPath` so the path classified here is exactly the
 * path the provider is sent (a `/v1/proxy` anywhere in the path is cut, as
 * upstream does: `/v1/chat/v1/proxy/completions` reaches `/v1/chat/completions`).
 */
export function stripProxyEndpointPath(pathname: string): string {
  const proxyEndpointPath =
    pathname.indexOf('/v1/proxy') > -1 ? '/v1/proxy' : '/v1';
  return pathname.replace(proxyEndpointPath, '');
}

/** The path the provider receives, as `ProviderContext.getProxyPath` cuts it. */
export function proxiedPath(requestURL: string): string {
  return stripProxyEndpointPath(new URL(requestURL).pathname).replace(
    /\/+$/,
    ''
  );
}

const isPlainObject = (v: unknown): v is Record<string, any> =>
  !!v &&
  typeof v === 'object' &&
  !Array.isArray(v) &&
  !(v instanceof FormData) &&
  !(v instanceof ArrayBuffer) &&
  !(v instanceof ReadableStream);

const isTextOrList = (v: unknown) =>
  typeof v === 'string' || (Array.isArray(v) && v.length > 0);

/**
 * Proxied API shapes the hooks understand: the path suffix (after any
 * provider prefix such as `/openai/v1`, `/anthropic/v1` or an Azure
 * deployment) and the body field the hooks read.
 */
const PROXY_HOOK_ROUTES: {
  path: RegExp;
  fn: endpointStrings;
  body: (b: Record<string, any>) => boolean;
}[] = [
  {
    path: /(^|\/)chat\/completions$/,
    fn: 'chatComplete',
    body: (b) => Array.isArray(b.messages) && b.messages.length > 0,
  },
  {
    path: /(^|\/)(completions|complete)$/,
    fn: 'complete',
    body: (b) => isTextOrList(b.prompt),
  },
  {
    // Not the Assistants `threads/{id}/messages`, which reads `content`.
    path: /^(?!.*\/threads\/).*(^|\/)messages$/,
    fn: 'messages',
    body: (b) => Array.isArray(b.messages) && b.messages.length > 0,
  },
  {
    path: /(^|\/)responses$/,
    fn: 'createModelResponse',
    body: (b) => isTextOrList(b.input),
  },
  {
    path: /(^|\/)embeddings$/,
    fn: 'embed',
    body: (b) => isTextOrList(b.input),
  },
];

/** Model listing / lookup: no prompt or generated content involved. */
const NO_CONTENT_GET = /^(\/[^/]+)*\/models(\/[^/]+)?$/;

export function classifyProxyRequest(
  c: Context,
  method: string,
  requestBody: unknown,
  requestHeaders: Record<string, string>
): ProxyHooksDecision {
  const path = proxiedPath(c.req.url);
  const upperMethod = method.toUpperCase();
  if (upperMethod === 'GET' && NO_CONTENT_GET.test(path)) {
    return { kind: 'noContent' };
  }
  if (upperMethod !== 'POST') {
    return {
      kind: 'unsupported',
      reason: `${upperMethod} requests cannot be inspected`,
    };
  }
  const contentType = requestHeaders['content-type']?.split(';')[0].trim();
  if (contentType !== CONTENT_TYPES.APPLICATION_JSON) {
    return {
      kind: 'unsupported',
      reason: `${contentType || 'body-less'} requests cannot be inspected`,
    };
  }
  if (!isPlainObject(requestBody)) {
    return { kind: 'unsupported', reason: 'the body is not a JSON object' };
  }
  const route = PROXY_HOOK_ROUTES.find((r) => r.path.test(path));
  if (!route) {
    return {
      kind: 'unsupported',
      reason: `the path ${path || '/'} is not a Chat Completions, Completions, Messages, Responses or Embeddings endpoint`,
    };
  }
  if (!route.body(requestBody)) {
    return {
      kind: 'unsupported',
      reason: `the body is not a ${route.fn} request`,
    };
  }
  return { kind: 'hooks', fn: route.fn };
}

/** The 400 returned before any upstream call. */
export function proxyGuardrailsUnsupportedResponse(reason: string): Response {
  const message =
    'Guardrails are configured for this target but cannot be applied to ' +
    `this proxied request (${reason}). Use the matching /v1 endpoint, or a ` +
    'target without guardrails.';
  return new Response(
    JSON.stringify({
      error: {
        message,
        type: 'invalid_request_error',
        param: null,
        code: 'unsupported_guardrail',
      },
    }),
    { status: 400, headers: { 'content-type': 'application/json' } }
  );
}
