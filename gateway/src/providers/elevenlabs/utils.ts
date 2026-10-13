import { ELEVENLABS } from '../../globals';
import { ErrorResponse } from '../types';
import { generateErrorResponse } from '../utils';

/**
 * ElevenLabs errors come as `{detail: {status, message}}`,
 * `{detail: [{msg, loc, type}]}` (validation) or `{detail: "..."}`.
 */
export const ElevenLabsErrorResponseTransform = (
  response: Record<string, any>
): ErrorResponse => {
  const detail = response?.detail;
  let message = 'unknown error';
  let code: string | null = null;
  if (typeof detail === 'string') {
    message = detail;
  } else if (Array.isArray(detail)) {
    message = detail
      .map((d) => (typeof d === 'string' ? d : d?.msg))
      .filter(Boolean)
      .join('; ');
  } else if (detail && typeof detail === 'object') {
    message = detail.message ?? JSON.stringify(detail);
    code = detail.status ?? detail.code ?? null;
  } else if (response?.error?.message) {
    message = response.error.message;
  } else if (response?.['html-message']) {
    message = response['html-message'];
  } else if (response) {
    message = JSON.stringify(response);
  }
  return generateErrorResponse(
    { message, type: null, param: null, code },
    ELEVENLABS
  );
};
