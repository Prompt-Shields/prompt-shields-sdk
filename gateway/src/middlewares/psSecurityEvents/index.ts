import { Context, Next } from 'hono';
import { env } from 'hono/adapter';
import type {
  GuardrailCheckResult,
  HookObject,
  HookResult,
  HookSpanContext,
} from '../hooks/types';
import { providerFromHeaders } from '../ps-telemetry';
import { attemptsOf, traceIdOf } from '../psAttempts';
import { isMetricsEnabled, labelValue, routeOf } from '../psMetrics/labels';
import {
  guardrailVerdictsTotal,
  piiDetectionsTotal,
  rateLimitedTotal,
  telemetryDroppedTotal,
} from '../psMetrics/security';
import {
  AuthFailureEvent,
  GuardrailFields,
  GuardrailStage,
  GuardrailVerdict,
  PayloadRejectedEvent,
  SECURITY_EVENT_SCHEMA_VERSION,
  SecurityEvent,
  SecurityEventBase,
  SecurityEventName,
} from './schema';

/**
 * PS gateway security events (schema v1, `./schema.ts`).
 *
 * `PS_SECURITY_EVENTS_LOG=true` (or `stdout`) writes one JSON line per event
 * to stdout, `stderr` to stderr; anything else, or unset, writes nothing.
 * Lines carry `"log.logger":"ps.security"` and are written by this module
 * only, never through `console`, so a log forwarder can route them apart from
 * the gateway's own log output.
 *
 * Writing never blocks the request: events are queued and written in one
 * batch on the next event-loop turn. When stdout cannot keep up the queue
 * waits for `drain`; past MAX_QUEUE the oldest events are dropped and
 * counted in `ps_gateway_telemetry_dropped_total{sink="security_events"}`.
 *
 * Guardrail events are recorded centrally by `HooksManager.executeHooks`
 * (every hook run on unified routes, proxied model calls and Responses goes
 * through it); 429s by the `psSecurityEvents()` middleware; auth failures and
 * payload rejections where the gateway refuses the request.
 */

type Target = 'stdout' | 'stderr';
export const MAX_QUEUE = 10_000;

export const securityLogTarget = (c: Context): Target | undefined => {
  const raw = env<Record<string, string | undefined>>(c)
    .PS_SECURITY_EVENTS_LOG?.trim()
    .toLowerCase();
  if (raw === 'true' || raw === 'stdout' || raw === '1') return 'stdout';
  if (raw === 'stderr') return 'stderr';
  return undefined;
};

// ---------------------------------------------------------------------------
// Non-blocking writer

let queue: { target: Target; line: string }[] = [];
let scheduled = false;
let waitingForDrain = false;

const nodeStream = (target: Target): any => {
  if (typeof process === 'undefined') return undefined;
  const s = (process as any)[target];
  return s && typeof s.write === 'function' ? s : undefined;
};

const schedule = () => {
  if (scheduled || waitingForDrain) return;
  scheduled = true;
  const run = () => {
    scheduled = false;
    flushSecurityEvents();
  };
  if (typeof setImmediate === 'function') setImmediate(run);
  else setTimeout(run, 0);
};

/**
 * Write everything queued now. Never throws. Exported for tests and shutdown;
 * `force` writes even while waiting for `drain` (the stream buffers it).
 */
export function flushSecurityEvents(force = false): void {
  if ((waitingForDrain && !force) || !queue.length) return;
  const batch = queue;
  queue = [];
  for (const target of ['stdout', 'stderr'] as Target[]) {
    const lines = batch.filter((e) => e.target === target).map((e) => e.line);
    if (!lines.length) continue;
    try {
      const stream = nodeStream(target);
      if (!stream) {
        // Workers: console output is collected asynchronously by the runtime.
        const log = target === 'stderr' ? console.error : console.log;
        for (const line of lines) log(line);
        continue;
      }
      const ok = stream.write(lines.join('\n') + '\n');
      if (ok === false && typeof stream.once === 'function') {
        waitingForDrain = true;
        stream.once('drain', () => {
          waitingForDrain = false;
          schedule();
        });
      }
    } catch {
      telemetryDroppedTotal.inc(
        { sink: 'security_events', reason: 'write_error' },
        lines.length
      );
    }
  }
}

function enqueue(target: Target, event: SecurityEvent) {
  queue.push({ target, line: JSON.stringify(event) });
  if (queue.length > MAX_QUEUE) {
    const n = queue.length - MAX_QUEUE;
    queue.splice(0, n);
    telemetryDroppedTotal.inc(
      { sink: 'security_events', reason: 'queue_full' },
      n
    );
  }
  schedule();
}

// ---------------------------------------------------------------------------
// Field helpers

const ID_RE = /^[A-Za-z0-9_.:@\/-]{1,128}$/;
const MODEL_RE = /^[A-Za-z0-9_.:@\/ +-]{1,128}$/;
const CATEGORY_RE = /^[A-Za-z0-9_.-]{1,64}$/;

/** A structured identifier, or "other" when it is not one. */
const safeId = (raw: unknown, re = ID_RE): string =>
  typeof raw === 'string' && re.test(raw) ? raw : 'other';

const headersOf = (c: Context): Record<string, string> =>
  Object.fromEntries(c.req.raw.headers) as Record<string, string>;

const REQUEST_ID_KEY = 'psSecurityRequestId';
const requestIdOf = (c: Context): string => {
  let id = c.get(REQUEST_ID_KEY as any) as string | undefined;
  if (!id) {
    id = traceIdOf(c.req.header('x-portkey-trace-id'));
    c.set(REQUEST_ID_KEY as any, id);
  }
  return id;
};

function base(
  c: Context,
  name: SecurityEventName,
  fields: Pick<
    SecurityEventBase,
    'event.kind' | 'event.category' | 'event.outcome' | 'severity' | 'action'
  > & { signature: string }
): SecurityEventBase {
  const headers = headersOf(c);
  const vars = env<Record<string, string | undefined>>(c);
  const event: SecurityEventBase = {
    '@timestamp': new Date().toISOString(),
    'ps.schema_version': SECURITY_EVENT_SCHEMA_VERSION,
    'event.id': crypto.randomUUID(),
    'event.name': name,
    'log.logger': 'ps.security',
    'service.name': 'prompt-shields-gateway',
    vendor_product: 'Prompt Shields Gateway',
    ...fields,
    'ps.request_id': requestIdOf(c),
    'http.route': routeOf(c),
    'http.request.method': c.req.method,
  };
  const tenant = vars.PS_SECURITY_EVENTS_TENANT;
  if (tenant) event['ps.tenant'] = labelValue(tenant);
  if (headers['x-ps-business-unit'])
    event['ps.business_unit'] = labelValue(headers['x-ps-business-unit']);
  if (headers['x-ps-environment'])
    event['ps.environment'] = labelValue(headers['x-ps-environment']);
  return event;
}

// ---------------------------------------------------------------------------
// Bounded metric labels

/** Distinct hook ids reported as metric labels before the rest become "other". */
export const MAX_HOOK_LABELS = 100;
/** Distinct PII categories reported as metric labels before the rest become "other". */
export const MAX_PII_CATEGORY_LABELS = 200;
const hookLabels = new Set<string>();
const piiLabels = new Set<string>();

const capped = (set: Set<string>, max: number, value: string): string => {
  if (value === 'other' || set.has(value)) return value;
  if (set.size >= max) return 'other';
  set.add(value);
  return value;
};

const has = (o: unknown, k: string) =>
  !!o && Object.prototype.hasOwnProperty.call(o, k);

/** The check id when it names a function in `registry` (the installed plugins), else "other". */
export const checkLabel = (id: unknown, registry: unknown): string => {
  if (typeof id !== 'string') return 'other';
  const [source, fn, ...rest] = id.split('.');
  return !rest.length &&
    has(registry, source) &&
    has((registry as any)[source], fn)
    ? id
    : 'other';
};

// `input_guardrails` / `output_guardrails` shorthand without an `id` gets a
// random suffix per request (convertHooksShorthand); one bucket per stage.
const SHORTHAND_HOOK_RE = /^(input|output)_guardrail_[a-z0-9]{0,5}$/;

/** Hook id as a metric label: shorthand ids bucketed, then capped. */
export const hookLabel = (id: unknown): string => {
  const shorthand = typeof id === 'string' && SHORTHAND_HOOK_RE.exec(id);
  if (shorthand) return `${shorthand[1]}_guardrail`;
  return capped(hookLabels, MAX_HOOK_LABELS, safeId(id));
};

const piiLabel = (category: string): string =>
  capped(piiLabels, MAX_PII_CATEGORY_LABELS, category);

/** Test helper: forget the label caps and the queue. */
export function _resetSecurityEventsForTests() {
  hookLabels.clear();
  piiLabels.clear();
  queue = [];
  scheduled = false;
  waitingForDrain = false;
}

// ---------------------------------------------------------------------------
// Guardrail results

/** Checks that detect personal data. */
export const PII_CHECKS = new Set([
  'portkey.pii',
  'azure.pii',
  'patronus.pii',
  'patronus.phi',
  'promptfoo.pii',
  'pangea.pii',
  'qualifire.pii',
]);

/** Checks that detect prompt injection or jailbreak attempts. */
export const INJECTION_CHECKS = new Set([
  'qualifire.promptInjections',
  'azure.shieldPrompt',
  'promptfoo.guard',
  'sydelabs.sydeguard',
]);

/**
 * PII categories and counts from a check's result data. Reads only category
 * names from the known result shapes (never the matched text, excerpts or
 * offsets); a category that is not a short identifier becomes "other".
 */
export function piiCategories(data: unknown): Map<string, number> {
  const counts = new Map<string, number>();
  const add = (raw: unknown) => {
    const category = safeId(raw, CATEGORY_RE);
    counts.set(category, (counts.get(category) ?? 0) + 1);
  };
  const fromEntities = (entities: unknown, key: string) => {
    if (!Array.isArray(entities)) return;
    for (const e of entities.slice(0, 1000)) add((e as any)?.[key]);
  };
  const visit = (d: any) => {
    if (!d || typeof d !== 'object') return;
    // portkey.pii: { detectedCategories: string[] }
    if (Array.isArray(d.detectedCategories))
      d.detectedCategories.slice(0, 1000).forEach(add);
    // azure.pii: [{ entities: [{ category }] }]
    fromEntities(d.entities, 'category');
    // promptfoo.pii / patronus.pii: { payload: { pii: [{ entity_type }] } }
    fromEntities(d.payload?.pii, 'entity_type');
  };
  if (Array.isArray(data)) data.slice(0, 100).forEach(visit);
  else visit(data);
  return counts;
}

const verdictOf = (check: GuardrailCheckResult, hook: HookObject) =>
  (check.error
    ? 'error'
    : check.verdict
      ? 'allow'
      : hook.deny
        ? 'deny'
        : 'flag') as GuardrailVerdict;

const VERDICT_SEVERITY: Record<
  GuardrailVerdict,
  SecurityEventBase['severity']
> = { allow: 'informational', deny: 'high', flag: 'medium', error: 'low' };

/**
 * Records one `executeHooks` run: a guardrail.verdict event and metric per
 * check, plus pii.detected / injection.detected events. Never throws.
 */
export function recordHookResults(
  c: Context,
  context: HookSpanContext,
  hooks: HookObject[],
  results: HookResult[],
  pluginRegistry: unknown
): void {
  try {
    const target = securityLogTarget(c);
    const metrics = isMetricsEnabled(c);
    if (!target && !metrics) return;

    const provider = context.provider ? safeId(context.provider) : undefined;
    const model =
      typeof context.request?.json?.model === 'string'
        ? safeId(context.request.json.model, MODEL_RE)
        : undefined;

    results.forEach((result, i) => {
      const hook = hooks[i];
      if (!hook || !result || result.skipped || !Array.isArray(result.checks))
        return;
      const stage: GuardrailStage =
        hook.eventType === 'afterRequestHook' ? 'output' : 'input';

      for (const check of result.checks) {
        const verdict = verdictOf(check, hook);
        // Whether this check made the hook deny the request (446): as in
        // executeEachHook, a failed check, or an errored one with failOnError.
        const blocked =
          !!hook.deny &&
          !check.verdict &&
          (!check.error || !!check.fail_on_error);
        const checkId = safeId(check.id);

        if (metrics) {
          guardrailVerdictsTotal.inc({
            hook: hookLabel(hook.id),
            check: checkLabel(check.id, pluginRegistry),
            verdict,
            stage,
          });
        }

        // PII: categories from the result; a failed PII check that names
        // none counts as one "unspecified" entity (unless it asserts absence).
        let pii: Map<string, number> | undefined;
        if (PII_CHECKS.has(check.id) && !check.error) {
          pii = piiCategories(check.data);
          const params = hook.checks?.find((x) => x.id === check.id)
            ?.parameters as Record<string, any> | undefined;
          if (!pii.size && !check.verdict && !params?.not)
            pii.set('unspecified', 1);
          if (metrics)
            for (const [category, n] of pii)
              piiDetectionsTotal.inc({ category: piiLabel(category) }, n);
        }
        const injection =
          INJECTION_CHECKS.has(check.id) && !check.error && !check.verdict;

        if (!target) continue;
        const guardrail: GuardrailFields = {
          'ps.guardrail.hook_id': safeId(hook.id),
          'ps.guardrail.hook_type':
            hook.type === 'mutator' ? 'mutator' : 'guardrail',
          'ps.guardrail.check_id': checkId,
          'ps.guardrail.stage': stage,
          'ps.guardrail.verdict': verdict,
        };
        const genAi = {
          ...(provider ? { 'gen_ai.provider.name': provider } : {}),
          ...(model ? { 'gen_ai.request.model': model } : {}),
        };
        const action = blocked ? 'blocked' : 'allowed';
        const outcome =
          verdict === 'allow' ? ('success' as const) : ('failure' as const);

        enqueue(target, {
          ...base(c, 'guardrail.verdict', {
            'event.kind': verdict === 'allow' ? 'event' : 'alert',
            'event.category': ['intrusion_detection'],
            'event.outcome': outcome,
            severity: blocked ? 'high' : VERDICT_SEVERITY[verdict],
            action,
            signature: checkId,
          }),
          ...genAi,
          ...guardrail,
          'event.name': 'guardrail.verdict',
          'ps.guardrail.deny': blocked,
          ...(Number.isFinite(check.execution_time)
            ? { 'ps.guardrail.duration_ms': check.execution_time }
            : {}),
        });

        for (const [category, count] of pii ?? []) {
          enqueue(target, {
            ...base(c, 'pii.detected', {
              'event.kind': 'alert',
              'event.category': ['intrusion_detection'],
              'event.outcome': outcome,
              severity: blocked ? 'high' : 'medium',
              action,
              signature: category,
            }),
            ...genAi,
            ...guardrail,
            'event.name': 'pii.detected',
            'ps.pii.category': category,
            'ps.pii.count': count,
          });
        }

        if (injection) {
          enqueue(target, {
            ...base(c, 'injection.detected', {
              'event.kind': 'alert',
              'event.category': ['intrusion_detection'],
              'event.outcome': outcome,
              severity: 'high',
              action,
              signature: checkId,
            }),
            ...genAi,
            ...guardrail,
            'event.name': 'injection.detected',
          });
        }
      }
    });
  } catch {
    // Security events are best-effort and must never affect the request.
  }
}

// ---------------------------------------------------------------------------
// Gateway refusals

/** A gateway credential check failed (admin console/log stream, /metrics or the MCP proxy). */
export function reportAuthFailure(
  c: Context,
  realm: AuthFailureEvent['ps.auth.realm'],
  reason: AuthFailureEvent['ps.auth.reason'],
  status = 401
): void {
  try {
    const target = securityLogTarget(c);
    if (!target) return;
    enqueue(target, {
      ...base(c, 'auth.failure', {
        'event.kind': 'event',
        'event.category': ['authentication'],
        'event.outcome': 'failure',
        severity: 'medium',
        action: 'failure',
        signature: reason,
      }),
      'event.name': 'auth.failure',
      'ps.auth.realm': realm,
      'ps.auth.reason': reason,
      'http.response.status_code': status,
    });
  } catch {
    /* best-effort */
  }
}

/** The gateway refused a request body before any upstream call. */
export function reportPayloadRejected(
  c: Context,
  reason: PayloadRejectedEvent['ps.payload.reason'],
  status: number
): void {
  try {
    const target = securityLogTarget(c);
    if (!target) return;
    const declared = Number(c.req.header('content-length'));
    enqueue(target, {
      ...base(c, 'payload.rejected', {
        'event.kind': 'event',
        'event.category': ['network'],
        'event.outcome': 'failure',
        severity: 'low',
        action: 'blocked',
        signature: reason,
      }),
      'event.name': 'payload.rejected',
      'ps.payload.reason': reason,
      'http.response.status_code': status,
      ...(c.req.header('content-length') !== undefined &&
      Number.isFinite(declared) &&
      declared >= 0
        ? { 'http.request.body.size': declared }
        : {}),
    });
  } catch {
    /* best-effort */
  }
}

/**
 * Middleware: records responses with status 429 (metric and rate_limit.hit
 * event). `upstream` when a provider attempt was made, else `gateway`.
 */
export function psSecurityEvents() {
  return async (c: Context, next: Next) => {
    await next();
    try {
      if (c.res.status !== 429) return;
      const target = securityLogTarget(c);
      const metrics = isMetricsEnabled(c);
      if (!target && !metrics) return;
      const source = attemptsOf(c).length ? 'upstream' : 'gateway';
      if (metrics) rateLimitedTotal.inc({ source });
      if (!target) return;
      const provider = providerFromHeaders(headersOf(c));
      enqueue(target, {
        ...base(c, 'rate_limit.hit', {
          'event.kind': 'event',
          'event.category': ['network'],
          'event.outcome': 'failure',
          severity: 'low',
          action: 'blocked',
          signature: `rate_limit.${source}`,
        }),
        ...(provider ? { 'gen_ai.provider.name': safeId(provider) } : {}),
        'event.name': 'rate_limit.hit',
        'ps.rate_limit.source': source,
        'http.response.status_code': 429,
      });
    } catch {
      /* best-effort */
    }
  };
}
