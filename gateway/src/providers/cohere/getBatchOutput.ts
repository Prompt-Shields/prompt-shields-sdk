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
    // PS fork (fix): check the status before reading the body; reading
    // .text() after .json() failed because the body was already used.
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
    const retrieveFileResponseJson: CohereGetFileResponse =
      await retrieveFileResponse.json();

    if (!retrieveFileResponseJson.dataset.dataset_parts) {
      throw new Error('file not found');
    }

    // PS fork (fix): parts are decoded one after another and the stream is
    // closed once, after the last part; closing it at the end of every part
    // made output with more than one part fail.
    const fileParts = retrieveFileResponseJson.dataset.dataset_parts;
    const encoder = new TextEncoder();
    const stream = new ReadableStream({
      start: async (controller) => {
        try {
          for (const filePart of fileParts) {
            const filePartRequest = cohereDatasetPartRequest(
              filePart.url,
              baseURL,
              headers,
              c
            );
            if (!filePartRequest) {
              throw new Error('Unexpected dataset part location');
            }
            const filePartResponse = await externalServiceFetch(
              filePart.url,
              filePartRequest
            );
            if (!filePartResponse.ok) {
              await filePartResponse.body?.cancel().catch(() => {});
              throw new Error(
                `cohere error: dataset part fetch failed (${filePartResponse.status})`
              );
            }
            const buf = Buffer.from(await filePartResponse.arrayBuffer());
            await new Promise<void>((resolve, reject) => {
              const decoder = new avro.streams.BlockDecoder({
                parseHook: (schema: any) => {
                  return avro.Type.forSchema(schema, { wrapUnions: true });
                },
              });
              decoder.on('data', (data: any) => {
                try {
                  controller.enqueue(
                    encoder.encode(
                      JSON.stringify(
                        CohereEmbedResponseTransformBatch(JSON.parse(data))
                      ) + '\n'
                    )
                  );
                } catch (e) {
                  decoder.destroy(e as Error);
                }
              });
              decoder.on('error', reject);
              decoder.on('end', () => resolve());
              // A no-op after 'end' or 'error'; otherwise it never finished.
              decoder.on('close', () =>
                reject(new Error('cohere error: dataset part ended early'))
              );
              decoder.end(buf);
            });
          }
          controller.close();
        } catch (e) {
          controller.error(e);
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
