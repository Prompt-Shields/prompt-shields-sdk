import { Context } from 'hono';
import AzureOpenAIAPIConfig from './api';
import { Options } from '../../types/requestBody';
import { RetrieveBatchResponse } from '../types';
import { AZURE_OPEN_AI } from '../../globals';
import { generateErrorResponse } from '../utils';
import { externalServiceFetch } from '../../utils/fetch';
import { logger } from '../../apm';
import { isSafeUpstreamURL } from '../../middlewares/requestValidator';

const AZURE_BLOB_HOST_SUFFIXES = [
  '.blob.core.windows.net',
  '.blob.core.usgovcloudapi.net',
  '.blob.core.chinacloudapi.cn',
];

const isAzureBlobURL = (rawUrl: string, c: Context) => {
  try {
    const url = new URL(rawUrl);
    const host = url.hostname.toLowerCase();
    return (
      url.protocol === 'https:' &&
      AZURE_BLOB_HOST_SUFFIXES.some((suffix) => host.endsWith(suffix)) &&
      isSafeUpstreamURL(rawUrl, c)
    );
  } catch {
    return false;
  }
};

// Return a ReadableStream containing batches output data
export const AzureOpenAIGetBatchOutputRequestHandler = async ({
  c,
  providerOptions,
  requestURL,
}: {
  c: Context;
  providerOptions: Options;
  requestURL: string;
}) => {
  // get batch details which has ouptut file id
  // get file content as ReadableStream
  // return file content
  const baseUrl = AzureOpenAIAPIConfig.getBaseURL({
    providerOptions,
    fn: 'retrieveBatch',
    c,
    gatewayRequestURL: requestURL,
  });
  const retrieveBatchRequestURL = requestURL.replace('/output', '');
  const retrieveBatchURL =
    baseUrl +
    AzureOpenAIAPIConfig.getEndpoint({
      providerOptions,
      fn: 'retrieveBatch',
      gatewayRequestURL: retrieveBatchRequestURL,
      c,
      gatewayRequestBodyJSON: {},
      gatewayRequestBody: {},
    });
  const retrieveBatchesHeaders = await AzureOpenAIAPIConfig.headers({
    c,
    providerOptions,
    fn: 'retrieveBatch',
    transformedRequestBody: {},
    transformedRequestUrl: retrieveBatchURL,
    gatewayRequestBody: {},
  });
  const retrieveBatchesResponse = await externalServiceFetch(retrieveBatchURL, {
    method: 'GET',
    headers: retrieveBatchesHeaders,
  });

  const batchDetails: RetrieveBatchResponse =
    await retrieveBatchesResponse.json();

  const outputFileId =
    batchDetails.output_file_id || batchDetails.error_file_id;
  const outputBlob = batchDetails.output_blob || batchDetails.error_blob;
  if (!outputFileId && !outputBlob) {
    const errors = batchDetails.errors;
    if (errors) {
      return new Response(JSON.stringify(errors), {
        status: 200,
      });
    }
    return new Response(
      JSON.stringify({
        error: 'invalid response output format',
        provider_response: batchDetails,
        provider: AZURE_OPEN_AI,
      }),
      {
        status: 400,
      }
    );
  }
  let response: Promise<Response> | null = null;
  if (outputFileId) {
    const retrieveFileContentRequestURL = `https://api.portkey.ai/v1/files/${outputFileId}/content`; // construct the entire url instead of the path of sanity sake
    const retrieveFileContentURL =
      baseUrl +
      AzureOpenAIAPIConfig.getEndpoint({
        providerOptions,
        fn: 'retrieveFileContent',
        gatewayRequestURL: retrieveFileContentRequestURL,
        c,
        gatewayRequestBodyJSON: {},
        gatewayRequestBody: {},
      });
    const retrieveFileContentHeaders = await AzureOpenAIAPIConfig.headers({
      c,
      providerOptions,
      fn: 'retrieveFileContent',
      transformedRequestBody: {},
      transformedRequestUrl: retrieveFileContentURL,
      gatewayRequestBody: {},
    });
    response = externalServiceFetch(retrieveFileContentURL, {
      method: 'GET',
      headers: retrieveFileContentHeaders,
    });
  }
  if (outputBlob) {
    // The blob URL comes from the provider response and is fetched with the
    // caller's Azure credentials, so only follow it to Azure Blob Storage.
    if (!isAzureBlobURL(outputBlob, c)) {
      return new Response(
        JSON.stringify(
          generateErrorResponse(
            {
              message: 'Unexpected batch output location',
              type: null,
              param: null,
              code: null,
            },
            AZURE_OPEN_AI
          )
        ),
        { status: 502, headers: { 'Content-Type': 'application/json' } }
      );
    }
    const retrieveBlobHeaders = await AzureOpenAIAPIConfig.headers({
      c,
      providerOptions: {
        ...providerOptions,
        azureEntraScope: 'https://storage.azure.com/.default',
      },
      fn: 'retrieveFileContent',
      transformedRequestBody: {},
      transformedRequestUrl: outputBlob,
      gatewayRequestBody: {},
    });
    response = externalServiceFetch(outputBlob, {
      method: 'GET',
      headers: {
        ...retrieveBlobHeaders,
        'x-ms-date': new Date().toUTCString(),
        'x-ms-version': '2022-11-02',
      },
    });
  }
  try {
    const responseData = await response;
    if (!responseData || !responseData.ok) {
      const errorResponse =
        (await responseData?.text()) || 'Failed to retrieve batch output';
      return new Response(
        JSON.stringify(
          generateErrorResponse(
            {
              message: errorResponse,
              type: null,
              param: null,
              code: null,
            },
            AZURE_OPEN_AI
          )
        ),
        {
          status: 400,
          headers: {
            'Content-Type': 'application/json',
          },
        }
      );
    }
    // Ported from Portkey 2.0.0: label the file as octet-stream so the
    // response handler streams it through instead of buffering it as JSON.
    return new Response(responseData.body, {
      headers: { 'Content-Type': 'application/octet-stream' },
    });
  } catch (error: any) {
    logger.error('Error in AzureOpenAIGetBatchOutputRequestHandler', error);
    return new Response(
      JSON.stringify(
        generateErrorResponse(
          {
            message: 'Unable to get batch output',
            type: null,
            param: null,
            code: null,
          },
          AZURE_OPEN_AI
        )
      ),
      {
        status: 500,
        headers: {
          'Content-Type': 'application/json',
        },
      }
    );
  }
};
