import Providers from '../providers';
import testVariables from './resources/testVariables';
import { executeChatCompletionEndpointTests } from './routeSpecificTestFunctions.ts/chatCompletion';

for (const provider in testVariables) {
  const variables = testVariables[provider];
  const config = Providers[provider];

  if (!variables.apiKey) {
    console.log(`Skipping ${provider} as API key is not provided`);
    continue;
  }

  if (config.chatComplete) {
    describe(`${provider} /chat/completions endpoint tests:`, () =>
      executeChatCompletionEndpointTests(provider, variables));
  }
}

// Runs without API keys, so the suite always has at least one test.
describe('provider test variables', () => {
  it('only reference registered providers', () => {
    for (const provider in testVariables) {
      expect(Providers[provider]).toBeDefined();
    }
  });
});
