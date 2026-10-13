import { SCALEWAY } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import ScalewayAPIConfig from './api';

const ScalewayConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: ScalewayAPIConfig,
  responseTransforms: responseTransformers(SCALEWAY, {
    chatComplete: true,
    embed: true,
  }),
};

export default ScalewayConfig;
