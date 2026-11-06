import { openAICompatibleAPIConfig } from '../openAICompatible';

// IONOS AI Model Hub, de-txl (OpenAI-compatible). The base URL is a fixed vendor host.
export const IONOS_BASE_URL = 'https://openai.inference.de-txl.ionos.com/v1';

const IONOSAPIConfig = openAICompatibleAPIConfig(IONOS_BASE_URL, true);

export default IONOSAPIConfig;
