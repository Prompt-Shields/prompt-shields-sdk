import { Context, Next } from 'hono';
import { env } from 'hono/adapter';

/**
 * Ported from Portkey gateway 2.0.0 (`src/middlewares/payloadSizeValidator.ts`).
 * PS fork changes: the limit is read per request from
 * `MAX_JSON_PAYLOAD_SIZE_IN_MB` (unset, zero or invalid = no limit, as
 * upstream), `application/json; charset=...` is matched too, and the metrics
 * bookkeeping that depends on upstream's Prometheus client is dropped.
 *
 * Only a declared `content-length` is checked; a chunked request body without
 * one passes through, as it does upstream.
 */
const BYTES_PER_MB = 1048576;

export const payloadSizeValidatorMiddleware = () => {
  return async (c: Context, next: Next) => {
    const limitMb = Number(
      env<Record<string, string | undefined>>(c).MAX_JSON_PAYLOAD_SIZE_IN_MB
    );
    const contentType = c.req.header('content-type') ?? '';
    if (
      limitMb > 0 &&
      contentType.split(';')[0].trim().toLowerCase() === 'application/json'
    ) {
      const payloadMb =
        Number(c.req.header('content-length') ?? 0) / BYTES_PER_MB;
      if (payloadMb > limitMb) {
        return c.json(
          {
            status: 'failure',
            message: `Payload size exceeds the limit of ${limitMb}MB`,
          },
          413
        );
      }
    }
    return next();
  };
};
