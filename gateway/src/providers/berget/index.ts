import { BERGET } from '../../globals';
import {
  chatCompleteParams,
  embedParams,
  responseTransformers,
} from '../open-ai-base';
import { ProviderConfigs } from '../types';
import BergetAPIConfig from './api';

const BergetConfig: ProviderConfigs = {
  chatComplete: chatCompleteParams([]),
  embed: embedParams([]),
  api: BergetAPIConfig,
  responseTransforms: responseTransformers(BERGET, {
    chatComplete: true,
    embed: true,
  }),
};

export default BergetConfig;
