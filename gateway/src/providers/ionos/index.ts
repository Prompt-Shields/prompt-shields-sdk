import { IONOS } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import IONOSAPIConfig from './api';

const IONOSConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: IONOSAPIConfig,
  responseTransforms: responseTransformers(IONOS, {
    chatComplete: true,
    embed: true,
  }),
};

export default IONOSConfig;
