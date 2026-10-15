import { Context, Next } from 'hono';
import { env } from 'hono/adapter';

/**
 * Ported from Portkey gateway 2.0.0 (`src/middlewares/payloadSizeValidator.ts`).
 * PS fork changes: the limit is read per request from
 * `MAX_JSON_PAYLOAD_SIZE_IN_MB` (unset, zero or invalid = no limit, as
 * upstream), `application/json; charset=...` is matched too, and the metrics
 * bookkeeping that depends on upstream's Prometheus client is dropped.
 *
 * Only a declared `content-length` is checked for JSON; a chunked request
 * body without one passes through, as it does upstream.
 *
 * PS addition (#75): `MAX_FILE_PAYLOAD_SIZE_IN_MB` limits `multipart/form-data`
 * uploads (audio transcriptions and translations, image edits, files). The
 * handlers read the whole form into memory before calling the provider, so
 * while this limit is set a multipart body must declare its
 * `content-length`; one without it gets 411 instead of being read unchecked.
 */
const BYTES_PER_MB = 1048576;

export const payloadSizeValidatorMiddleware = () => {
  return async (c: Context, next: Next) => {
    const vars = env<Record<string, string | undefined>>(c);
    const contentType = (c.req.header('content-type') ?? '')
      .split(';')[0]
      .trim()
      .toLowerCase();
    const declared = c.req.header('content-length');

    const limitMb = Number(vars.MAX_JSON_PAYLOAD_SIZE_IN_MB);
    if (limitMb > 0 && contentType === 'application/json') {
      const payloadMb = Number(declared ?? 0) / BYTES_PER_MB;
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

    const fileLimitMb = Number(vars.MAX_FILE_PAYLOAD_SIZE_IN_MB);
    if (fileLimitMb > 0 && contentType === 'multipart/form-data') {
      const bytes = declared?.trim() ? Number(declared) : NaN;
      if (!Number.isFinite(bytes) || bytes < 0) {
        return c.json(
          {
            status: 'failure',
            message: 'Content-Length is required for multipart uploads',
          },
          411
        );
      }
      if (bytes / BYTES_PER_MB > fileLimitMb) {
        return c.json(
          {
            status: 'failure',
            message: `Upload size exceeds the limit of ${fileLimitMb}MB`,
          },
          413
        );
      }
    }
    return next();
  };
};
