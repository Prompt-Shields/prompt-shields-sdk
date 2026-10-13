import { ProviderConfigs } from '../types';
import ElevenLabsAPIConfig from './api';
import {
  ElevenLabsCreateSpeechConfig,
  ElevenLabsCreateSpeechResponseTransform,
} from './createSpeech';
import {
  ElevenLabsCreateTranscriptionRequestHandler,
  ElevenLabsCreateTranscriptionResponseTransform,
} from './createTranscription';

const ElevenLabsConfig: ProviderConfigs = {
  api: ElevenLabsAPIConfig,
  createSpeech: ElevenLabsCreateSpeechConfig,
  createTranscription: {},
  requestHandlers: {
    createTranscription: ElevenLabsCreateTranscriptionRequestHandler,
  },
  responseTransforms: {
    createSpeech: ElevenLabsCreateSpeechResponseTransform,
    createTranscription: ElevenLabsCreateTranscriptionResponseTransform,
  },
};

export default ElevenLabsConfig;
