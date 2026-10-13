import { MINIMAX } from '../../globals';
import { chatCompleteParams, responseTransformers } from '../open-ai-base';
import { ProviderConfigs } from '../types';
import MiniMaxAPIConfig from './api';

const MiniMaxConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  api: MiniMaxAPIConfig,
  responseTransforms: responseTransformers(MINIMAX, {
    chatComplete: true,
  }),
};

export default MiniMaxConfig;
