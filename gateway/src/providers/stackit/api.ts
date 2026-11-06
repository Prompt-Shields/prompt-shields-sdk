import { openAICompatibleAPIConfig } from '../openAICompatible';

// STACKIT AI Model Serving, eu01 (OpenAI-compatible). The base URL is a fixed vendor host.
export const STACKIT_BASE_URL =
  'https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1';

const STACKITAPIConfig = openAICompatibleAPIConfig(STACKIT_BASE_URL, true);

export default STACKITAPIConfig;
