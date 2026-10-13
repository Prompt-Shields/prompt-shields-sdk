import { BYTEPLUS } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import BytePlusAPIConfig from './api';

const BytePlusConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: BytePlusAPIConfig,
  responseTransforms: responseTransformers(BYTEPLUS, {
    chatComplete: true,
    embed: true,
  }),
};

export default BytePlusConfig;
