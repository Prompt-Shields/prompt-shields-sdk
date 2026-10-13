import { DEEPGRAM } from '../../globals';
import { ErrorResponse } from '../types';
import { generateErrorResponse } from '../utils';

/**
 * Deepgram errors come as `{err_code, err_msg, request_id}` or
 * `{category, message, details, request_id}`.
 */
export const DeepgramErrorResponseTransform = (
  response: Record<string, any>
): ErrorResponse => {
  const message =
    response?.err_msg ??
    response?.message ??
    response?.details ??
    response?.error?.message ??
    response?.['html-message'] ??
    JSON.stringify(response ?? 'unknown error');
  const code = response?.err_code ?? response?.category ?? null;
  return generateErrorResponse(
    { message: String(message), type: null, param: null, code },
    DEEPGRAM
  );
};
