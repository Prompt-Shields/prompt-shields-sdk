import { LIGHTNING_AI } from '../../globals';
import { chatCompleteParams, responseTransformers } from '../open-ai-base';
import { ProviderConfigs } from '../types';
import LightningAIAPIConfig from './api';

const LightningAIConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  api: LightningAIAPIConfig,
  responseTransforms: responseTransformers(LIGHTNING_AI, {
    chatComplete: true,
  }),
};

export default LightningAIConfig;
