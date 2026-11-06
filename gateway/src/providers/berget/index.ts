import { BERGET } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { BERGET_BASE_URL } from './api';

const BergetConfig = openAICompatibleProvider({
  provider: BERGET,
  baseURL: BERGET_BASE_URL,
  embed: true,
});

export default BergetConfig;
