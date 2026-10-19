import { Counter, defaultRegistry } from './registry';

/**
 * Security and telemetry-health metrics (security events v1). Kept apart
 * from `./index` so that senders (psOtel, psTelemetrySender) can count drops
 * without importing the request middleware.
 *
 * Label values are bounded by the callers: check ids are known plugin ids or
 * "other", hook ids are capped, PII categories are sanitised and capped, and
 * drop reasons come from fixed lists.
 */
export const guardrailVerdictsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_guardrail_verdicts_total',
    'Guardrail check results by hook id, check id, verdict (allow, deny, flag, error) and stage (input, output). Unknown check ids and hook ids past the cap are "other".',
    ['hook', 'check', 'verdict', 'stage']
  )
);
export const piiDetectionsTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_pii_detections_total',
    'PII entities detected by guardrail checks, by category. Values are never recorded.',
    ['category']
  )
);
export const rateLimitedTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_rate_limited_total',
    'Requests answered with 429, by source: upstream (a provider) or gateway.',
    ['source']
  )
);
export const telemetryDroppedTotal = defaultRegistry.register(
  new Counter(
    'ps_gateway_telemetry_dropped_total',
    'Telemetry items dropped before delivery, by sink (otel, ps_telemetry, security_events) and reason.',
    ['sink', 'reason']
  )
);
