import { Context } from 'hono';
import { getRuntimeKey } from 'hono/adapter';
import { redactString } from '../../utils/redact';
import { findPiiSpans, piiPlaceholder, redactPii } from '../router/pii';

let logId = 0;
const MAX_RESPONSE_LENGTH = 100000;

// Map to store all connected log clients
const logClients: Map<string | number, any> = new Map();

const addLogClient = (clientId: any, client: any) => {
  logClients.set(clientId, client);
};

const removeLogClient = (clientId: any) => {
  logClients.delete(clientId);
};

const sanitizeHeaders = (headers: Record<string, unknown> = {}) =>
  Object.fromEntries(Object.keys(headers).map((key) => [key, '[REDACTED]']));

const ALLOWED_PROVIDER_OPTION_KEYS = new Set([
  'provider',
  'overrideParams',
  'retry',
  'cache',
  'requestURL',
  'rubeusURL',
]);

const sanitizeProviderOptions = (
  providerOptions: Record<string, unknown> = {}
) =>
  Object.fromEntries(
    Object.entries(providerOptions).map(([key, value]) => [
      key,
      ALLOWED_PROVIDER_OPTION_KEYS.has(key)
        ? // PS fork: URLs can carry keys in the query (e.g. Google `?key=`).
          typeof value === 'string'
          ? redactString(value)
          : value
        : '[REDACTED]',
    ])
  );

const MAX_REDACT_DEPTH = 64;
// Integers this long can be a card number sent as a JSON number.
const MIN_CARD_DIGITS = 13;

/**
 * PS fork (#337): a copy of `value` with PII in every string replaced by
 * category placeholders (`[EMAIL]`, `[CREDIT_CARD]`, ...; see redactPii in
 * router/pii.ts). Integers of 13+ digits are checked as their digits and
 * replaced by the placeholder string when they hold a card number. Only
 * arrays and plain objects are walked; other objects (Date, Response) are
 * kept as they are (Date serialises to a timestamp, Response to `{}`).
 * Anything nested deeper than MAX_REDACT_DEPTH is dropped.
 */
export const redactPiiDeep = (value: unknown, depth = 0): unknown => {
  if (typeof value === 'string') return redactPii(value);
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) return value;
    const digits = String(Math.abs(value));
    if (digits.length < MIN_CARD_DIGITS) return value;
    // Card numbers only: a millisecond timestamp has a phone number's shape.
    return findPiiSpans(digits).some((s) => s.category === 'credit_card')
      ? piiPlaceholder('credit_card')
      : value;
  }
  if (!value || typeof value !== 'object') return value;
  if (depth >= MAX_REDACT_DEPTH) return '[REDACTED]';
  if (Array.isArray(value))
    return value.map((item) => redactPiiDeep(item, depth + 1));
  // By tag, not prototype: a parsed body can come from another realm.
  if (Object.prototype.toString.call(value) !== '[object Object]') return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      redactPiiDeep(item, depth + 1),
    ])
  );
};

export const sanitizeRequestOption = (requestOption: any) => {
  if (!requestOption || typeof requestOption !== 'object') return requestOption;

  const sanitizedOption = { ...requestOption };

  if (
    sanitizedOption.providerOptions &&
    typeof sanitizedOption.providerOptions === 'object'
  ) {
    sanitizedOption.providerOptions = sanitizeProviderOptions(
      sanitizedOption.providerOptions as Record<string, unknown>
    );
  }

  if (
    sanitizedOption.transformedRequest &&
    typeof sanitizedOption.transformedRequest === 'object'
  ) {
    sanitizedOption.transformedRequest = {
      ...sanitizedOption.transformedRequest,
    };
    if (sanitizedOption.transformedRequest.headers) {
      sanitizedOption.transformedRequest.headers = sanitizeHeaders(
        sanitizedOption.transformedRequest.headers as Record<string, unknown>
      );
    }
  }

  if (
    sanitizedOption.responseHeaders &&
    typeof sanitizedOption.responseHeaders === 'object'
  ) {
    sanitizedOption.responseHeaders = sanitizeHeaders(
      sanitizedOption.responseHeaders as Record<string, unknown>
    );
  }

  // PS fork (#337): request and response bodies (requestParams,
  // transformedRequest, finalUntransformedRequest, originalResponse,
  // response) and any other field go out with PII replaced by categories.
  return redactPiiDeep(sanitizedOption);
};

/**
 * PS fork (#337): the log entries broadcast for one call. The first entry
 * carries the final response (`response`, parsed JSON or a placeholder for
 * a stream), redacted before it is cut to MAX_RESPONSE_LENGTH so a cut
 * never splits a value the detector would have found. The shared
 * requestOptions are not modified.
 */
export const buildLogRequestOptions = (
  requestOptionsArray: any[],
  response: unknown
) =>
  requestOptionsArray.map((option, index) => {
    if (index !== 0 || response === undefined)
      return sanitizeRequestOption(option);
    const sanitized = sanitizeRequestOption({ ...option, response });
    const responseString = JSON.stringify(sanitized.response);
    if (responseString && responseString.length > MAX_RESPONSE_LENGTH) {
      sanitized.response =
        responseString.substring(0, MAX_RESPONSE_LENGTH) + '...';
    }
    return sanitized;
  });

const broadcastLog = async (log: any) => {
  const message = {
    data: log,
    event: 'log',
    id: String(logId++),
  };

  const deadClients: any = [];

  // Run all sends in parallel
  await Promise.all(
    Array.from(logClients.entries()).map(async ([id, client]) => {
      try {
        await Promise.race([
          client.sendLog(message),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error('Send timeout')), 1000)
          ),
        ]);
      } catch (error: any) {
        console.error(`Failed to send log to client ${id}:`, error.message);
        deadClients.push(id);
      }
    })
  );

  // Remove dead clients after iteration
  deadClients.forEach((id: any) => {
    removeLogClient(id);
  });
};

async function processLog(c: Context, start: number) {
  const ms = Date.now() - start;
  if (!c.req.url.includes('/v1/')) return;
  // PS fork (#337): redaction scans every body; skip it when nobody tails.
  if (!logClients.size) return;

  const requestOptionsArray = c.get('requestOptions');
  if (!requestOptionsArray?.length) {
    return;
  }

  let response: unknown;
  try {
    response = requestOptionsArray[0].requestParams?.stream
      ? { message: 'The response was a stream.' }
      : await c.res.clone().json();
  } catch (error) {
    console.error('Error processing log:', error);
  }

  const endpoint = c.req.url.split(':8787')[1];
  await broadcastLog(
    JSON.stringify({
      time: new Date().toLocaleString(),
      method: c.req.method,
      endpoint: endpoint === undefined ? endpoint : redactPii(endpoint),
      status: c.res.status,
      duration: ms,
      requestOptions: buildLogRequestOptions(requestOptionsArray, response),
    })
  );
}

export const logHandler = () => {
  return async (c: Context, next: any) => {
    c.set('addLogClient', addLogClient);
    c.set('removeLogClient', removeLogClient);

    const start = Date.now();

    await next();

    const runtime = getRuntimeKey();

    if (runtime == 'workerd') {
      c.executionCtx.waitUntil(processLog(c, start));
    } else if (['node', 'bun', 'deno'].includes(runtime)) {
      processLog(c, start).then().catch(console.error);
    }
  };
};
