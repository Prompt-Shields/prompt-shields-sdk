import { REGOLO } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { REGOLO_BASE_URL } from './api';

const RegoloConfig = openAICompatibleProvider({
  provider: REGOLO,
  baseURL: REGOLO_BASE_URL,
  embed: true,
});

export default RegoloConfig;
