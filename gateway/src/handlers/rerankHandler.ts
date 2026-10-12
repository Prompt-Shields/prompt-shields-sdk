import { RouterError } from '../errors/RouterError';
import {
  constructConfigFromRequestHeaders,
  tryTargetsRecursively,
} from './handlerUtils';
import { Context } from 'hono';

/**
 * Handles the '/v1/rerank' API request by selecting the appropriate
 * provider(s) and making the request to them. Ported from Portkey gateway
 * 2.0.0 and adapted to this fork's handler style (request body and headers
 * read from the Hono request rather than upstream's context helpers).
 *
 * @param {Context} c - The Hono context.
 * @returns {Promise<Response>} - The response from the provider.
 */
export async function rerankHandler(c: Context): Promise<Response> {
  try {
    const request = await c.req.json();
    const requestHeaders = Object.fromEntries(c.req.raw.headers);
    const camelCaseConfig = constructConfigFromRequestHeaders(requestHeaders);

    return await tryTargetsRecursively(
      c,
      camelCaseConfig,
      request,
      requestHeaders,
      'rerank',
      'POST',
      'config'
    );
  } catch (err: any) {
    console.error('rerankHandler error: ', err?.message);
    const isRouterError = err instanceof RouterError;
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: isRouterError ? err.message : 'Something went wrong',
      }),
      {
        status: isRouterError ? 400 : 500,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }
}
