import { ProviderConfigs } from '../types';
import DeepgramAPIConfig from './api';
import {
  DeepgramCreateSpeechConfig,
  DeepgramCreateSpeechResponseTransform,
} from './createSpeech';
import {
  DeepgramCreateTranscriptionRequestHandler,
  DeepgramCreateTranscriptionResponseTransform,
} from './createTranscription';

const DeepgramConfig: ProviderConfigs = {
  api: DeepgramAPIConfig,
  createSpeech: DeepgramCreateSpeechConfig,
  createTranscription: {},
  requestHandlers: {
    createTranscription: DeepgramCreateTranscriptionRequestHandler,
  },
  responseTransforms: {
    createSpeech: DeepgramCreateSpeechResponseTransform,
    createTranscription: DeepgramCreateTranscriptionResponseTransform,
  },
};

export default DeepgramConfig;
