import { Context, HonoRequest } from 'hono';
import { CONTENT_TYPES } from '../globals';
import {
  constructConfigFromRequestHeaders,
  tryTargetsRecursively,
} from './handlerUtils';
import { RouterError } from '../errors/RouterError';

// PS fork: read through Hono's request (HonoRequest) so a body that a
// middleware already parsed (psRouter reads every JSON POST) comes from the
// body cache instead of the consumed raw stream.
async function getRequestData(request: HonoRequest, contentType: string) {
  // Body-less requests (and any content type we do not parse) proxy with
  // empty params. Most SDKs send GET/DELETE without a content-type, which
  // previously left the body undefined and crashed tryPost on params.stream.
  let finalRequest: any = {};
  if (['GET', 'DELETE'].includes(request.method)) {
    return finalRequest;
  }
  if (contentType == CONTENT_TYPES.APPLICATION_JSON) {
    finalRequest = await request.json();
  } else if (contentType == CONTENT_TYPES.MULTIPART_FORM_DATA) {
    finalRequest = await request.formData();
  } else if (contentType?.startsWith(CONTENT_TYPES.GENERIC_AUDIO_PATTERN)) {
    finalRequest = await request.arrayBuffer();
  }

  return finalRequest;
}

export async function proxyHandler(c: Context): Promise<Response> {
  try {
    let requestHeaders = Object.fromEntries(c.req.raw.headers);
    const requestContentType = requestHeaders['content-type']?.split(';')[0];

    const request = await getRequestData(c.req, requestContentType);

    const camelCaseConfig = constructConfigFromRequestHeaders(requestHeaders);

    const tryTargetsResponse = await tryTargetsRecursively(
      c,
      camelCaseConfig,
      request,
      requestHeaders,
      'proxy',
      c.req.method,
      'config'
    );

    return tryTargetsResponse;
  } catch (err: any) {
    console.error('proxyHandler error: ', err);
    let statusCode = 500;
    let errorMessage = `Proxy error: ${err.message}`;

    if (err instanceof RouterError) {
      statusCode = 400;
      errorMessage = err.message;
    }

    return new Response(
      JSON.stringify({
        status: 'failure',
        message: errorMessage,
      }),
      {
        status: statusCode,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }
}
