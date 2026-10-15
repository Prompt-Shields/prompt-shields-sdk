import { META } from '../../globals';
import { chatCompleteParams, responseTransformers } from '../open-ai-base';
import { ProviderConfigs } from '../types';
import MetaAPIConfig from './api';

const MetaConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  api: MetaAPIConfig,
  responseTransforms: responseTransformers(META, {
    chatComplete: true,
  }),
};

export default MetaConfig;
