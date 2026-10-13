import { REGOLO } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import RegoloAPIConfig from './api';

const RegoloConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: RegoloAPIConfig,
  responseTransforms: responseTransformers(REGOLO, {
    chatComplete: true,
    embed: true,
  }),
};

export default RegoloConfig;
