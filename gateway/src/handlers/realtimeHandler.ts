import { Context } from 'hono';
import { constructConfigFromRequestHeaders } from './handlerUtils';
import { ProviderAPIConfig } from '../providers/types';
import Providers from '../providers';
import { Options } from '../types/requestBody';
import {
  addListeners,
  getOptionsForOutgoingConnection,
  getURLForOutgoingConnection,
} from './websocketUtils';
import { RealtimeLlmEventParser } from '../services/realtimeLlmEventParser';
import { GatewayError } from '../errors/GatewayError';

const getOutgoingWebSocket = async (url: string, options: RequestInit) => {
  let outgoingWebSocket: WebSocket | null = null;
  try {
    let response = await fetch(url, options);
    outgoingWebSocket = response.webSocket;
  } catch (error) {
    console.error('getOutgoingWebSocket error: ', error);
  }

  if (!outgoingWebSocket) {
    throw new Error('WebSocket connection failed');
  }

  outgoingWebSocket.accept();
  return outgoingWebSocket;
};

export async function realTimeHandler(c: Context): Promise<Response> {
  try {
    const requestHeaders = Object.fromEntries(c.req.raw.headers);

    const providerOptions = constructConfigFromRequestHeaders(
      requestHeaders
    ) as Options;
    const provider = providerOptions.provider ?? '';
    const apiConfig: ProviderAPIConfig = Providers[provider].api;
    const url = await getURLForOutgoingConnection(
      apiConfig,
      providerOptions,
      c.req.url,
      c
    );
    const options = await getOptionsForOutgoingConnection(
      apiConfig,
      providerOptions,
      url,
      c
    );

    const sessionOptions = {
      id: crypto.randomUUID(),
      providerOptions: {
        ...providerOptions,
        requestURL: url,
        rubeusURL: 'realtime',
      },
      requestHeaders,
      requestParams: {},
    };

    const webSocketPair = new WebSocketPair();
    const client = webSocketPair[0];
    const server = webSocketPair[1];

    server.accept();

    let outgoingWebSocket: WebSocket = await getOutgoingWebSocket(url, options);
    const eventParser = new RealtimeLlmEventParser();
    addListeners(outgoingWebSocket, eventParser, server, c, sessionOptions);

    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  } catch (err: any) {
    console.error('realtimeHandler error: ', err.message);
    // PS fork (security): a refused upstream URL is the caller's error.
    if (err instanceof GatewayError) {
      return new Response(
        JSON.stringify({ status: 'failure', message: err.message }),
        { status: 400, headers: { 'content-type': 'application/json' } }
      );
    }
    return new Response(
      JSON.stringify({
        status: 'failure',
        message: 'Something went wrong',
      }),
      {
        status: 500,
        headers: {
          'content-type': 'application/json',
        },
      }
    );
  }
}
