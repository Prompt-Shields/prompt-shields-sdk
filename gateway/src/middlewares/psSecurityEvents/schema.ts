/**
 * Prompt Shields gateway security-event schema, v1.
 *
 * One JSON object per event, written as one line. Field names follow the
 * OpenTelemetry semantic conventions (`http.*`, `gen_ai.*`, `event.name`) and
 * ECS (`event.*`, `log.logger`) where one exists, plus the Splunk CIM fields
 * `action`, `signature`, `severity` and `vendor_product`, so a forwarder
 * needs no field extraction. Product fields use the `ps.` prefix.
 *
 * Privacy contract: an event never carries prompt or response content, PII
 * values, header values other than the X-PS business context, credentials,
 * client IP addresses or the caller's email. PII appears as a category and a
 * count only.
 *
 * Compatibility: within v1, fields may be added but never renamed, removed
 * or given a new meaning. The JSON Schema is `docs/security-events.schema.json`.
 */

export const SECURITY_EVENT_SCHEMA_VERSION = '1.0.0';

export type SecurityEventName =
  | 'guardrail.verdict'
  | 'pii.detected'
  | 'injection.detected'
  | 'auth.failure'
  | 'rate_limit.hit'
  | 'payload.rejected';

/** CIM severity. */
export type SecuritySeverity = 'informational' | 'low' | 'medium' | 'high';

/**
 * CIM action: `allowed` / `blocked` (Intrusion Detection, DLP, Alerts) or
 * `failure` (Authentication).
 */
export type SecurityAction = 'allowed' | 'blocked' | 'failure';

/** allow: passed; deny: failed and the hook denies; flag: failed, request continues; error: the check errored. */
export type GuardrailVerdict = 'allow' | 'deny' | 'flag' | 'error';

/** input: before the provider call (request); output: after it (response). */
export type GuardrailStage = 'input' | 'output';

export interface SecurityEventBase {
  /** ISO 8601 UTC time the event was recorded. */
  '@timestamp': string;
  'ps.schema_version': typeof SECURITY_EVENT_SCHEMA_VERSION;
  /** Random id of this event. */
  'event.id': string;
  'event.name': SecurityEventName;
  /** ECS: `alert` for detections and denials, `event` otherwise. */
  'event.kind': 'alert' | 'event';
  /** ECS categories, e.g. `intrusion_detection`, `authentication`, `network`. */
  'event.category': string[];
  /** ECS: `success` (allowed), `failure` (blocked, failed, errored) or `unknown`. */
  'event.outcome': 'success' | 'failure' | 'unknown';
  /** The logger name; filter on it to route these lines. */
  'log.logger': 'ps.security';
  'service.name': 'prompt-shields-gateway';
  vendor_product: 'Prompt Shields Gateway';
  severity: SecuritySeverity;
  action: SecurityAction;
  /** What fired: a check id, a PII category, or a rejection reason. */
  signature: string;
  /** Request id: the caller's `x-portkey-trace-id` when it is a short token, else generated once per request. */
  'ps.request_id': string;
  /** Matched route pattern, e.g. `/v1/chat/completions`. */
  'http.route': string;
  'http.request.method': string;
  /** Tenant of this gateway deployment (`PS_SECURITY_EVENTS_TENANT`), when set. */
  'ps.tenant'?: string;
  /** Workspace within the tenant: the `X-PS-Business-Unit` header. */
  'ps.business_unit'?: string;
  /** `X-PS-Environment` header. */
  'ps.environment'?: string;
  'gen_ai.provider.name'?: string;
  'gen_ai.request.model'?: string;
}

export interface GuardrailFields {
  'ps.guardrail.hook_id': string;
  'ps.guardrail.hook_type': 'guardrail' | 'mutator';
  'ps.guardrail.check_id': string;
  'ps.guardrail.stage': GuardrailStage;
  'ps.guardrail.verdict': GuardrailVerdict;
}

export interface GuardrailVerdictEvent
  extends SecurityEventBase,
    GuardrailFields {
  'event.name': 'guardrail.verdict';
  /** True when the hook denies on failure (the request got a 446). */
  'ps.guardrail.deny': boolean;
  'ps.guardrail.duration_ms'?: number;
}

export interface PiiDetectedEvent extends SecurityEventBase, GuardrailFields {
  'event.name': 'pii.detected';
  /** Category as the check reports it (e.g. `EMAIL_ADDRESS`), or `unspecified`. */
  'ps.pii.category': string;
  /** Number of entities of this category the check found. */
  'ps.pii.count': number;
}

export interface InjectionDetectedEvent
  extends SecurityEventBase,
    GuardrailFields {
  'event.name': 'injection.detected';
}

export interface AuthFailureEvent extends SecurityEventBase {
  'event.name': 'auth.failure';
  /** What was protected: the admin console/log stream, or `/metrics`. */
  'ps.auth.realm': 'admin' | 'metrics';
  'ps.auth.reason': 'missing_credentials' | 'invalid_credentials';
  'http.response.status_code': number;
}

export interface RateLimitHitEvent extends SecurityEventBase {
  'event.name': 'rate_limit.hit';
  /** `upstream`: a provider answered 429; `gateway`: the gateway did. */
  'ps.rate_limit.source': 'upstream' | 'gateway';
  'http.response.status_code': 429;
}

export interface PayloadRejectedEvent extends SecurityEventBase {
  'event.name': 'payload.rejected';
  'ps.payload.reason':
    | 'payload_too_large'
    | 'length_required'
    | 'unsupported_guardrail';
  'http.response.status_code': number;
  /** Declared `content-length`, when there was one. */
  'http.request.body.size'?: number;
}

export type SecurityEvent =
  | GuardrailVerdictEvent
  | PiiDetectedEvent
  | InjectionDetectedEvent
  | AuthFailureEvent
  | RateLimitHitEvent
  | PayloadRejectedEvent;
