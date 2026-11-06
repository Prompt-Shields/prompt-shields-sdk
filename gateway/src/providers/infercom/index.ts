import { INFERCOM } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { INFERCOM_BASE_URL } from './api';

const InfercomConfig = openAICompatibleProvider({
  provider: INFERCOM,
  baseURL: INFERCOM_BASE_URL,
});

export default InfercomConfig;
