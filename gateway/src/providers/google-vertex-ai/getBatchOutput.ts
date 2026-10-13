import { getAnthropicChatCompleteResponseTransform } from '../anthropic/chatComplete';
import { RequestHandler } from '../types';
import { GoogleBatchRecord } from './types';
import { getModelAndProvider, isEmbeddingModel } from './utils';
import { responseTransformers } from '../open-ai-base';
import {
  GoogleChatCompleteResponseTransform,
  VertexLlamaChatCompleteResponseTransform,
} from './chatComplete';
import { BatchEndpoints, GOOGLE_VERTEX_AI } from '../../globals';
import { createLineSplitter } from '../../handlers/streamHandlerUtils';
import GoogleApiConfig from './api';
import { GoogleEmbedResponseTransform } from './embed';
import { externalServiceFetch } from '../../utils/fetch';
import { generateErrorResponse } from '../utils';
import { logger } from '../../apm';

const responseTransforms = {
  google: {
    [BatchEndpoints.CHAT_COMPLETIONS]: GoogleChatCompleteResponseTransform,
    [BatchEndpoints.EMBEDDINGS]: GoogleEmbedResponseTransform,
  },
  anthropic: {
    [BatchEndpoints.CHAT_COMPLETIONS]:
      getAnthropicChatCompleteResponseTransform(GOOGLE_VERTEX_AI),
    [BatchEndpoints.EMBEDDINGS]: null,
  },
  meta: {
    [BatchEndpoints.CHAT_COMPLETIONS]: VertexLlamaChatCompleteResponseTransform,
    [BatchEndpoints.EMBEDDINGS]: null,
  },
  endpoints: {
    [BatchEndpoints.CHAT_COMPLETIONS]: responseTransformers(GOOGLE_VERTEX_AI, {
      chatComplete: true,
    }).chatComplete,
    [BatchEndpoints.EMBEDDINGS]: responseTransformers(GOOGLE_VERTEX_AI, {
      embed: true,
    }).embed,
  },
};

type TransformFunction = (
  response: unknown,
  responseStatus: number,
  headers: Record<string, string>,
  strictOpenAiCompliance: boolean,
  gatewayRequestUrl: string,
  gatewayRequest: Params
) => Record<string, unknown>;

const getOpenAIBatchRow = ({
  row,
  batchId,
  transform,
  endpoint,
  modelName,
}: {
  row: Record<string, unknown>;
  transform: TransformFunction;
  batchId: string;
  endpoint: BatchEndpoints;
  modelName: string;
}) => {
  const response =
    endpoint === BatchEndpoints.EMBEDDINGS
      ? ((row ?? {}) as Record<string, unknown>)
      : ((row['response'] ?? {}) as Record<string, unknown>);
  const id = `batch-${batchId}${response.responseId ? `-${response.responseId}` : ''}`;

  let error = null;
  try {
    error = JSON.parse(row.status as string);
  } catch {
    error = row.status;
  }

  return {
    id,
    custom_id:
      row.requestId || (row?.instance as any)?.requestId || response.responseId,
    response: {
      ...(!error && { status_code: 200 }),
      request_id: id,
      body:
        !error && transform(response, 200, {}, false, '', { model: modelName }),
    },
    error: error,
  };
};

const headObject = async (url: string, headers: Record<string, string>) => {
  try {
    const response = await externalServiceFetch(url, {
      method: 'HEAD',
      headers,
    });
    return response.status === 200;
  } catch {
    return false;
  }
};

const errorResponse = (message: string, status: number) =>
  new Response(
    JSON.stringify(
      generateErrorResponse(
        { message, type: null, param: null, code: null },
        GOOGLE_VERTEX_AI
      )
    ),
    { status, headers: { 'Content-Type': 'application/json' } }
  );

export const BatchOutputRequestHandler: RequestHandler = async ({
  requestURL,
  providerOptions,
  c,
  requestBody,
}) => {
  const headers = await GoogleApiConfig.headers({
    c,
    fn: 'retrieveBatch',
    providerOptions,
    transformedRequestBody: requestBody,
    transformedRequestUrl: requestURL,
  });

  const options = {
    method: 'GET',
    headers,
  };

  // URL: <gateway>/v1/batches/<batchId>/output
  const batchId = requestURL.split('/').at(-2);

  const batchDetailsURL = requestURL.replace(/\/output$/, '');

  const baseURL = await GoogleApiConfig.getBaseURL({
    c,
    providerOptions,
    fn: 'retrieveBatch',
    gatewayRequestURL: batchDetailsURL,
  });

  const endpoint = GoogleApiConfig.getEndpoint({
    c,
    providerOptions,
    fn: 'retrieveBatch',
    gatewayRequestURL: batchDetailsURL,
    gatewayRequestBodyJSON: {},
  });

  const batchesURL = `${baseURL}${endpoint}`;
  let modelName = '';
  let outputURL;
  // Ported from Portkey 2.0.0: provider errors come back as error responses
  // instead of exceptions.
  try {
    const response = await externalServiceFetch(batchesURL, options);
    if (!response.ok) {
      return errorResponse(await response.text(), response.status);
    }
    const data = (await response.json()) as GoogleBatchRecord;
    outputURL = data.outputInfo?.gcsOutputDirectory ?? '';
    modelName = data.model;
  } catch (error) {
    return errorResponse(
      (error as Error).message || 'Failed to retrieve batch output',
      500
    );
  }

  if (!outputURL) {
    return errorResponse('Failed to retrieve batch details', 500);
  }

  const { provider } = getModelAndProvider(modelName ?? '');

  const batchEndpoint = isEmbeddingModel(modelName)
    ? BatchEndpoints.EMBEDDINGS
    : BatchEndpoints.CHAT_COMPLETIONS;

  const providerConfigMap =
    responseTransforms[provider as keyof typeof responseTransforms];
  const providerConfig =
    providerConfigMap?.[batchEndpoint] ??
    responseTransforms['endpoints'][batchEndpoint];

  if (!providerConfig) {
    throw new Error(
      `Endpoint ${batchEndpoint} not supported for provider ${provider}`
    );
  }

  outputURL = outputURL.replace('gs://', 'https://storage.googleapis.com/');

  // Ported from Portkey 2.0.0: chat jobs write `predictions.jsonl` and
  // embedding jobs `000000000000.jsonl`, so probe for the file that exists.
  let predictionFileId = 'predictions.jsonl';
  if (!(await headObject(`${outputURL}/predictions.jsonl`, headers))) {
    if (await headObject(`${outputURL}/000000000000.jsonl`, headers)) {
      predictionFileId = '000000000000.jsonl';
    }
  }

  const outputResponse = await externalServiceFetch(
    `${outputURL}/${predictionFileId}`,
    options
  );

  if (!outputResponse.ok || !outputResponse.body) {
    return errorResponse(
      (await outputResponse.text()) || 'Failed to retrieve batch output',
      outputResponse.ok ? 500 : outputResponse.status
    );
  }

  const decoder = new TextDecoder();
  const encoder = new TextEncoder();

  // Prepare a transform stream to process complete lines.
  const responseStream = new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      let buffer;
      try {
        const json = JSON.parse(decoder.decode(chunk));
        const row = getOpenAIBatchRow({
          row: json,
          batchId: batchId ?? '',
          transform: providerConfig as TransformFunction,
          endpoint: batchEndpoint,
          modelName,
        });
        buffer = JSON.stringify(row);
      } catch (error) {
        logger.error('Failed to transform vertex batch output line', {
          error,
        });
        return;
      }
      controller.enqueue(encoder.encode(buffer + '\n'));
    },
  });

  // Pipe the provider stream through the line splitter and then to the
  // response stream. A client or socket hang-up rejects the pipe; catch it so
  // it cannot surface as an unhandled rejection.
  outputResponse.body
    .pipeThrough(createLineSplitter())
    .pipeTo(responseStream.writable)
    .catch(() => {});

  return new Response(responseStream.readable, {
    headers: { 'Content-Type': 'application/octet-stream' },
  });
};

export const BatchOutputResponseTransform = async (response: Response) => {
  return response;
};
