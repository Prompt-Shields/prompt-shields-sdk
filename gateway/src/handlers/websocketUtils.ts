import { Context } from 'hono';
import { ProviderAPIConfig } from '../providers/types';
import { Options } from '../types/requestBody';
import { RealtimeLlmEventParser } from '../services/realtimeLlmEventParser';
import { GatewayError } from '../errors/GatewayError';
import { isSafeUpstreamURL } from '../middlewares/requestValidator';
import { UPSTREAM_REDIRECT_MODE } from '../utils/fetch';
import { assertProxyURLOrigin } from './services/providerContext';

export const addListeners = (
  outgoingWebSocket: WebSocket,
  eventParser: RealtimeLlmEventParser,
  server: WebSocket,
  c: Context,
  sessionOptions: any
) => {
  outgoingWebSocket.addEventListener('message', (event) => {
    server?.send(event.data as string);
    try {
      const parsedData = JSON.parse(event.data as string);
      eventParser.handleEvent(c, parsedData, sessionOptions);
    } catch (err) {
      console.error('outgoingWebSocket message parse error: ', event);
    }
  });

  const errorListener = (event: ErrorEvent) => {
    console.error('outgoingWebSocket error: ', event);
    server?.close();
  };

  outgoingWebSocket.addEventListener('close', (event) => {
    // 1005 is a normal close event.
    server.removeEventListener('error', errorListener);
    if (event.code === 1005) {
      server?.close();
      return;
    }
    server?.close(event.code, event.reason);
  });

  outgoingWebSocket.addEventListener('error', (event) => {
    console.error('outgoingWebSocket error: ', event);
    server?.close();
  });

  server.addEventListener('message', (event) => {
    outgoingWebSocket?.send(event.data as string);
  });

  server.addEventListener('close', () => {
    outgoingWebSocket?.close();
  });

  server.addEventListener('error', errorListener);
};

export const getOptionsForOutgoingConnection = async (
  apiConfig: ProviderAPIConfig,
  providerOptions: Options,
  url: string,
  c: Context
) => {
  const headers = await apiConfig.headers({
    c,
    providerOptions,
    fn: 'realtime',
    transformedRequestUrl: url,
    transformedRequestBody: {},
  });
  headers['Upgrade'] = 'websocket';
  headers['Connection'] = 'Keep-Alive';
  headers['Keep-Alive'] = 'timeout=600';
  return {
    headers,
    method: 'GET',
    // PS fork (security): never follow an upstream redirect.
    redirect: UPSTREAM_REDIRECT_MODE,
  };
};

/**
 * PS fork (security): the upstream realtime URL gets the same checks as an
 * HTTP upstream call. Provider base URLs interpolate request fields (Azure
 * resource name, Foundry URL) and endpoints carry the model or deployment, so
 * the URL must pass isSafeUpstreamURL and keep the configured base's origin.
 * Returns the https/http URL; see toWebSocketURL for the ws form.
 */
export const getURLForOutgoingConnection = async (
  apiConfig: ProviderAPIConfig,
  providerOptions: Options,
  gatewayRequestURL: string,
  c: Context
): Promise<string> => {
  const baseUrl = await apiConfig.getBaseURL({
    providerOptions,
    c,
    gatewayRequestURL,
  });
  const endpoint = apiConfig.getEndpoint({
    c,
    providerOptions,
    fn: 'realtime',
    gatewayRequestBodyJSON: {},
    gatewayRequestURL: gatewayRequestURL,
  });
  const url = `${baseUrl}${endpoint}`;
  if (!baseUrl || !isSafeUpstreamURL(url, c)) {
    throw new GatewayError('Invalid upstream URL', 400);
  }
  assertProxyURLOrigin(url, baseUrl, 'Invalid upstream URL');
  return url;
};

/** `https:` -> `wss:` and `http:` -> `ws:`; any other scheme is refused. */
export const toWebSocketURL = (url: string): string => {
  const parsed = new URL(url);
  if (parsed.protocol === 'https:') parsed.protocol = 'wss:';
  else if (parsed.protocol === 'http:') parsed.protocol = 'ws:';
  else throw new GatewayError('Invalid upstream URL', 400);
  return parsed.toString();
};
