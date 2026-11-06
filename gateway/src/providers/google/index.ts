import { ProviderConfigs } from '../types';
import GoogleApiConfig from './api';
import {
  GoogleChatCompleteConfig,
  GoogleChatCompleteResponseTransform,
  GoogleChatCompleteStreamChunkTransform,
  GoogleErrorResponseTransform,
} from './chatComplete';
import {
  geminiMessagesCountTokensResponseTransform,
  googleMessagesCountTokensConfig,
} from './countTokens';
import { GOOGLE } from '../../globals';
import { GoogleEmbedConfig, GoogleEmbedResponseTransform } from './embed';

const GoogleConfig: ProviderConfigs = {
  api: GoogleApiConfig,
  chatComplete: GoogleChatCompleteConfig,
  embed: GoogleEmbedConfig,
  // PS fork (#73 follow-up): Messages count_tokens via Gemini countTokens.
  messagesCountTokens: googleMessagesCountTokensConfig(
    GoogleChatCompleteConfig
  ),
  responseTransforms: {
    chatComplete: GoogleChatCompleteResponseTransform,
    'stream-chatComplete': GoogleChatCompleteStreamChunkTransform,
    embed: GoogleEmbedResponseTransform,
    messagesCountTokens: geminiMessagesCountTokensResponseTransform(
      GOOGLE,
      GoogleErrorResponseTransform
    ),
  },
};

export default GoogleConfig;
