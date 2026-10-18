import { CLAUDE_PLATFORM_AWS } from '../../globals';
import {
  AnthropicChatCompleteConfig,
  getAnthropicChatCompleteResponseTransform,
  getAnthropicStreamChunkTransform,
} from '../anthropic/chatComplete';
import {
  AnthropicMessagesConfig,
  AnthropicMessagesResponseTransform,
} from '../anthropic/messages';
import { ProviderConfigs } from '../types';
import ClaudePlatformAwsAPIConfig from './api';

// Request and response shapes are the first-party Claude API's, so the
// Anthropic transforms are reused unchanged; only host, auth and headers differ.
const ClaudePlatformAwsConfig: ProviderConfigs = {
  chatComplete: AnthropicChatCompleteConfig,
  messages: AnthropicMessagesConfig,
  messagesCountTokens: AnthropicMessagesConfig,
  api: ClaudePlatformAwsAPIConfig,
  responseTransforms: {
    chatComplete:
      getAnthropicChatCompleteResponseTransform(CLAUDE_PLATFORM_AWS),
    'stream-chatComplete':
      getAnthropicStreamChunkTransform(CLAUDE_PLATFORM_AWS),
    messages: AnthropicMessagesResponseTransform,
  },
};

export default ClaudePlatformAwsConfig;
