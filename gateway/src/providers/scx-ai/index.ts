import { SCX_AI } from '../../globals';
import { chatCompleteParams, responseTransformers } from '../open-ai-base';
import { ProviderConfigs } from '../types';
import SCXAIAPIConfig from './api';

const SCXAIConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  api: SCXAIAPIConfig,
  responseTransforms: responseTransformers(SCX_AI, {
    chatComplete: true,
  }),
};

export default SCXAIConfig;
