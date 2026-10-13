import { Params } from '../../types/requestBody';
import { ErrorResponse, ProviderConfig } from '../types';
import { generateErrorResponse } from '../utils';

/**
 * `POST /v1/ocr` request config. The gateway speaks the Mistral OCR request
 * shape, which Mistral, Azure AI Foundry and Vertex-hosted Mistral share.
 */
export const MistralAIOcrConfig: ProviderConfig = {
  model: {
    param: 'model',
    required: true,
    default: 'mistral-ocr-latest',
    // Vertex addresses publisher models as `mistralai.<model>`.
    transform: (params: Params) => params.model?.replace(/^mistralai\./, ''),
  },
  id: { param: 'id' },
  document: { param: 'document', required: true },
  pages: { param: 'pages' },
  include_image_base64: { param: 'include_image_base64' },
  image_limit: { param: 'image_limit' },
  image_min_size: { param: 'image_min_size' },
  bbox_annotation_format: { param: 'bbox_annotation_format' },
  document_annotation_format: { param: 'document_annotation_format' },
};

/** OCR response: Mistral shape, with errors normalised for each host. */
export const GetMistralAIOcrResponseTransform = (provider: string) => {
  return (
    response: Record<string, any>,
    responseStatus: number
  ): Record<string, any> | ErrorResponse => {
    if (responseStatus === 200 && Array.isArray(response?.pages)) {
      return { ...response, provider };
    }
    const detail = response?.detail;
    const message =
      response?.error?.message ??
      response?.message ??
      (typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: any) => d?.msg ?? String(d)).join('; ')
          : undefined) ??
      JSON.stringify(response);
    return generateErrorResponse(
      {
        message,
        type: response?.error?.type ?? response?.type ?? null,
        param: null,
        code: response?.error?.code ?? response?.code ?? null,
      },
      provider
    );
  };
};
