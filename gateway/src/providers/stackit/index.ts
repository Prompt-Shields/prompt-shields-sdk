import { STACKIT } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import STACKITAPIConfig from './api';

const STACKITConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: STACKITAPIConfig,
  responseTransforms: responseTransformers(STACKIT, {
    chatComplete: true,
    embed: true,
  }),
};

export default STACKITConfig;
