import { ProviderConfigs } from '../types';
import FalAIAPIConfig from './api';
import {
  FalAIImageGenerateConfig,
  FalAIImageGenerateResponseTransform,
} from './imageGenerate';

// Images map to /v1/images/generations. Video and audio models have no
// unified route; they are reached through the proxy (`POST /v1/<model id>`).
const FalAIConfig: ProviderConfigs = {
  api: FalAIAPIConfig,
  imageGenerate: FalAIImageGenerateConfig,
  responseTransforms: {
    imageGenerate: FalAIImageGenerateResponseTransform,
  },
};

export default FalAIConfig;
