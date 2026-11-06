import { IONOS } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { IONOS_BASE_URL } from './api';

const IONOSConfig = openAICompatibleProvider({
  provider: IONOS,
  baseURL: IONOS_BASE_URL,
  embed: true,
});

export default IONOSConfig;
