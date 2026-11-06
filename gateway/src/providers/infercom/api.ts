import { openAICompatibleAPIConfig } from '../openAICompatible';

// Infercom, EU inference in German data centres (OpenAI-compatible). The base
// URL is from the vendor's getting-started page; no API reference was found,
// so only chat completions are routed (embeddings unverified).
export const INFERCOM_BASE_URL = 'https://api.infercom.ai/v1';

const InfercomAPIConfig = openAICompatibleAPIConfig(INFERCOM_BASE_URL);

export default InfercomAPIConfig;
