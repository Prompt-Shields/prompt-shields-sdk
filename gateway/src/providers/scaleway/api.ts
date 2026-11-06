import { openAICompatibleAPIConfig } from '../openAICompatible';

// Scaleway Generative APIs, fr-par (OpenAI-compatible). The base URL is a fixed vendor host.
export const SCALEWAY_BASE_URL = 'https://api.scaleway.ai/v1';

const ScalewayAPIConfig = openAICompatibleAPIConfig(SCALEWAY_BASE_URL, true);

export default ScalewayAPIConfig;
