import { Context } from 'hono';
import CohereAPIConfig from './api';
import { Options } from '../../types/requestBody';
import { CohereGetFileResponse, CohereRetrieveBatchResponse } from './types';
import avro from 'avsc';
import { CohereEmbedResponseTransformBatch } from './embed';
import { COHERE } from '../../globals';
import { externalServiceFetch } from '../../utils/fetch';
import { isSafeUpstreamURL } from '../../middlewares/requestValidator';

/**
 * PS fork (security): dataset part URLs come from the Cohere response and are
 * usually presigned storage URLs, so the Cohere API key goes only to the
 * Cohere API origin; any other https host is fetched without it.
 */
export const cohereDatasetPartRequest = (
  rawUrl: string,
  cohereBaseURL: string,
  headers: Record<string, string>,
  c: Context
): RequestInit | null => {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || !isSafeUpstreamURL(rawUrl, c)) return null;
  if (url.origin === new URL(cohereBaseURL).origin) {
    return { method: 'GET', headers };
  }
  return { method: 'GET' };
};

export const CohereGetBatchOutputHandler = async ({
  c,
  providerOptions,
  requestURL,
}: {
  c: Context;
  providerOptions: Options;
  requestURL: string;
}) => {
  try {
    // get the base url and endpoint for retrieveBatch
    const baseURL = await CohereAPIConfig.getBaseURL({
      providerOptions,
      fn: 'retrieveBatch',
      c,
      gatewayRequestURL: requestURL,
    });
    const endpoint = CohereAPIConfig.getEndpoint({
      providerOptions,
      fn: 'retrieveBatch',
      gatewayRequestURL: requestURL.replace('/output', ''),
      c,
      gatewayRequestBodyJSON: {},
    });
    const headers = await CohereAPIConfig.headers({
      providerOptions,
      fn: 'retrieveBatch',
      c,
      transformedRequestUrl: baseURL + endpoint,
      transformedRequestBody: {},
    });
    // get the batch details
    const retrieveBatchResponse = await externalServiceFetch(
      baseURL + endpoint,
      {
        method: 'GET',
        headers,
      }
    );
    if (!retrieveBatchResponse.ok) {
      const errorText = await retrieveBatchResponse.text();
      throw new Error(
        JSON.stringify({
          type: 'provider_error',
          code: retrieveBatchResponse.status,
          param: null,
          message: 'cohere error: ' + errorText,
        })
      );
    }
    const batchDetails: CohereRetrieveBatchResponse =
      await retrieveBatchResponse.json();
    const outputFileId = batchDetails.output_dataset_id;

    // get the file details
    const retrieveFileResponse = await externalServiceFetch(
      `https://api.cohere.ai/v1/datasets/${outputFileId}`,
      {
        method: 'GET',
        headers,
      }
    );
    const retrieveFileResponseJson: CohereGetFileResponse =
      await retrieveFileResponse.json();
    if (!retrieveFileResponse.ok) {
      const errorText = await retrieveFileResponse.text();
      throw new Error(
        JSON.stringify({
          type: 'provider_error',
          code: retrieveFileResponse.status,
          param: null,
          message: 'cohere error: ' + errorText,
        })
      );
    }

    if (!retrieveFileResponseJson.dataset.dataset_parts) {
      throw new Error('file not found');
    }

    const stream = new ReadableStream({
      start: async (controller) => {
        const fileParts = retrieveFileResponseJson.dataset.dataset_parts;
        for (const filePart of fileParts) {
          const filePartRequest = cohereDatasetPartRequest(
            filePart.url,
            baseURL,
            headers,
            c
          );
          if (!filePartRequest) {
            controller.error(new Error('Unexpected dataset part location'));
            return;
          }
          const filePartResponse = await externalServiceFetch(
            filePart.url,
            filePartRequest
          );
          const arrayBuffer = await filePartResponse.arrayBuffer();
          const buf = Buffer.from(arrayBuffer);
          const decoder = new avro.streams.BlockDecoder({
            parseHook: (schema: any) => {
              return avro.Type.forSchema(schema, { wrapUnions: true });
            },
          });
          const encoder = new TextEncoder();
          decoder.on('data', (data: any) => {
            controller.enqueue(
              encoder.encode(
                JSON.stringify(
                  CohereEmbedResponseTransformBatch(JSON.parse(data))
                )
              )
            );
            controller.enqueue(encoder.encode('\n'));
          });
          decoder.on('end', () => {
            controller.close();
          });
          decoder.end(buf);
        }
      },
    });
    return new Response(stream, {
      headers: { 'Content-Type': 'application/octet-stream' },
    });
  } catch (error: any) {
    let errorResponse;

    try {
      errorResponse = JSON.parse(error.message);
      errorResponse.provider = COHERE;
    } catch (_e) {
      errorResponse = {
        error: {
          message: error.message,
          type: null,
          param: null,
          code: 500,
        },
        provider: COHERE,
      };
    }
    return new Response(JSON.stringify(errorResponse), {
      status: 500,
      headers: {
        'Content-Type': 'application/json',
      },
    });
  }
};
