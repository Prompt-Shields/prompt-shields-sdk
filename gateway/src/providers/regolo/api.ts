import { openAICompatibleAPIConfig } from '../openAICompatible';

// Regolo.ai by Seeweb, Italy (OpenAI-compatible). The base URL is a fixed vendor host.
export const REGOLO_BASE_URL = 'https://api.regolo.ai/v1';

const RegoloAPIConfig = openAICompatibleAPIConfig(REGOLO_BASE_URL, true);

export default RegoloAPIConfig;
