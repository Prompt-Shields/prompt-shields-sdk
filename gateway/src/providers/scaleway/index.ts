import { SCALEWAY } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { SCALEWAY_BASE_URL } from './api';

const ScalewayConfig = openAICompatibleProvider({
  provider: SCALEWAY,
  baseURL: SCALEWAY_BASE_URL,
  embed: true,
});

export default ScalewayConfig;
