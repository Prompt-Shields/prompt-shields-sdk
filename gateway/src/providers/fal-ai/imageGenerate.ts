import { FAL_AI } from '../../globals';
import { ErrorResponse, ImageGenerateResponse, ProviderConfig } from '../types';
import {
  generateErrorResponse,
  generateInvalidProviderResponseError,
} from '../utils';

/**
 * OpenAI `size` (`WxH`) becomes fal's `image_size` object; a fal preset
 * (`square_hd`, `landscape_16_9`, ...) is passed through; `auto` is left to
 * the model's default.
 */
function falImageSize(size: unknown) {
  if (typeof size !== 'string' || !size || size === 'auto') return undefined;
  const m = /^(\d+)x(\d+)$/i.exec(size);
  if (m) return { width: parseInt(m[1], 10), height: parseInt(m[2], 10) };
  return size;
}

export const FalAIImageGenerateConfig: ProviderConfig = {
  prompt: { param: 'prompt', required: true },
  n: { param: 'num_images', min: 1 },
  size: {
    param: 'image_size',
    transform: (params: any) => falImageSize(params.size),
  },
  output_format: { param: 'output_format' },
  // b64_json: fal returns the images inline as data URIs.
  response_format: {
    param: 'sync_mode',
    transform: (params: any) =>
      params.response_format === 'b64_json' ? true : undefined,
  },
  // Inputs fal's image models commonly take.
  seed: { param: 'seed' },
  negative_prompt: { param: 'negative_prompt' },
  num_inference_steps: { param: 'num_inference_steps' },
  guidance_scale: { param: 'guidance_scale' },
  aspect_ratio: { param: 'aspect_ratio' },
  enable_safety_checker: { param: 'enable_safety_checker' },
};

interface FalImage {
  url: string;
  width?: number;
  height?: number;
  content_type?: string;
}

interface FalImageGenerateResponse {
  images: FalImage[];
  seed?: number;
  prompt?: string;
  has_nsfw_concepts?: boolean[];
  timings?: Record<string, number>;
}

interface FalErrorResponse {
  detail: string | { msg?: string; loc?: unknown[]; type?: string }[];
}

function falErrorMessage(detail: FalErrorResponse['detail']): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) =>
        [Array.isArray(d?.loc) ? d.loc.join('.') : '', d?.msg]
          .filter(Boolean)
          .join(': ')
      )
      .join('; ');
  }
  return JSON.stringify(detail);
}

const DATA_URI_RE = /^data:[^;,]+;base64,/;

export const FalAIImageGenerateResponseTransform: (
  response: FalImageGenerateResponse | FalErrorResponse,
  responseStatus: number
) => ImageGenerateResponse | ErrorResponse = (response, responseStatus) => {
  if (
    responseStatus !== 200 ||
    !response ||
    !Array.isArray((response as FalImageGenerateResponse).images)
  ) {
    if (response && 'detail' in response) {
      return generateErrorResponse(
        {
          message: falErrorMessage(response.detail),
          type: null,
          param: null,
          code: null,
        },
        FAL_AI
      );
    }
    return generateInvalidProviderResponseError(response, FAL_AI);
  }

  return {
    created: Math.floor(Date.now() / 1000),
    data: (response as FalImageGenerateResponse).images.map((image) =>
      typeof image?.url === 'string' && DATA_URI_RE.test(image.url)
        ? { b64_json: image.url.replace(DATA_URI_RE, '') }
        : { url: image?.url }
    ),
    provider: FAL_AI,
  };
};
