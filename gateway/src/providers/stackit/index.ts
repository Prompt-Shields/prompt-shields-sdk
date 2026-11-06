import { STACKIT } from '../../globals';
import { openAICompatibleProvider } from '../openAICompatible';
import { STACKIT_BASE_URL } from './api';

const STACKITConfig = openAICompatibleProvider({
  provider: STACKIT,
  baseURL: STACKIT_BASE_URL,
  embed: true,
});

export default STACKITConfig;
