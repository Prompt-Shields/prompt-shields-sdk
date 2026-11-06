import { openAICompatibleAPIConfig } from '../openAICompatible';

// Berget AI, Sweden (OpenAI-compatible). The base URL is a fixed vendor host.
export const BERGET_BASE_URL = 'https://api.berget.ai/v1';

const BergetAPIConfig = openAICompatibleAPIConfig(BERGET_BASE_URL, true);

export default BergetAPIConfig;
