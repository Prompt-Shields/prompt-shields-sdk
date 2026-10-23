/**
 * PS fork (security): an errored guardrail check never passes on its own
 * verdict. The hook verdict is `verdict || (error && !failOnError)`, so a
 * plugin returning `verdict: true` with an error made `failOnError` a no-op
 * and let the request through. On error a plugin returns `verdict: false`
 * and `failOnError` alone decides: true denies, false fails open.
 */
// HooksManager loads every plugin; `jose` (default.jwt) ships ESM only.
jest.mock('jose', () => ({}));

import { HooksManager } from '../src/middlewares/hooks';
import { HookType } from '../src/middlewares/hooks/types';
import { PluginEgressError } from './egress';
import * as utils from './utils';
import { handler as azureContentSafety } from './azure/contentSafety';
import { handler as azureShieldPrompt } from './azure/shieldPrompt';
import { handler as azureProtectedMaterial } from './azure/protectedMaterial';
import { handler as azurePii } from './azure/pii';
import { pluginHandler as bedrockHandler } from './bedrock/index';
import { mistralGuardrailHandler } from './mistral/index';
import { handler as walledaiHandler } from './walledai/walledprotect';
import { handler as promptfooGuard } from './promptfoo/guard';
import { handler as promptfooHarm } from './promptfoo/harm';
import { handler as promptfooPii } from './promptfoo/pii';
import { handler as f5Scan } from './f5-guardrails/scan';
import { handler as exaOnline } from './exa/online';
import { handler as pangeaTextGuard } from './pangea/textGuard';
import { handler as pangeaPii } from './pangea/pii';
import { handler as crowdstrikeHandler } from './crowdstrike-aidr/guardChatCompletion';
import { handler as javelinHandler } from './javelin/guardrails';
import { handler as acuvityScan } from './acuvity/scan';
import { handler as panwIntercept } from './panw-prisma-airs/intercept';
import { handler as pillarScanPrompt } from './pillar/scanPrompt';
import { handler as patronusToxicity } from './patronus/toxicity';
import { handler as patronusPii } from './patronus/pii';
import { handler as portkeyPii } from './portkey/pii';
import { handler as portkeyGibberish } from './portkey/gibberish';
import { handler as qualifirePolicy } from './qualifire/policy';
import { handler as qualifireGrounding } from './qualifire/grounding';
import { handler as regexReplace } from './default/regexReplace';
import { handler as addPrefix } from './default/addPrefix';

const context = {
  request: {
    text: 'hello',
    json: { messages: [{ role: 'user', content: 'hello' }] },
  },
  response: { text: '', json: {} },
  requestType: 'chatComplete' as const,
};
const emptyContext = {
  request: { text: '', json: {} },
  response: { text: '', json: {} },
  requestType: 'chatComplete' as const,
};
const options = { env: {} } as any;

afterEach(() => {
  jest.restoreAllMocks();
});

function quiet() {
  jest.spyOn(console, 'log').mockImplementation(() => {});
  jest.spyOn(console, 'error').mockImplementation(() => {});
}

/** Every vendor POST is refused as a blocked egress (#220). */
function refuseEgress() {
  jest
    .spyOn(utils, 'post')
    .mockRejectedValue(new PluginEgressError('egress refused'));
}

type Case = [
  name: string,
  run: () => Promise<{ error?: any; verdict?: boolean }>,
];

function expectErroredNotPassed(result: { error?: any; verdict?: boolean }) {
  expect(result.error).toBeTruthy();
  expect(result.verdict).not.toBe(true);
}

describe('azure plugins return verdict false on error', () => {
  const cases: Case[] = [
    [
      'contentSafety: missing credentials',
      () => azureContentSafety(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'contentSafety: refused egress (customHost on a private address)',
      () =>
        azureContentSafety(
          context,
          {
            credentials: {
              contentSafety: {
                resourceName: 'r',
                apiKey: 'k',
                customHost: 'http://10.0.0.5',
              },
            },
          } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'contentSafety: managed identity disabled (token denied)',
      () =>
        azureContentSafety(
          context,
          {
            credentials: {
              contentSafety: { resourceName: 'r', azureAuthMode: 'managed' },
            },
          } as any,
          'beforeRequestHook',
          options
        ),
    ],
    [
      'shieldPrompt: wrong hook',
      () => azureShieldPrompt(context, {} as any, 'afterRequestHook'),
    ],
    [
      'shieldPrompt: resourceName rewriting the host is refused',
      () =>
        azureShieldPrompt(
          context,
          {
            credentials: {
              contentSafety: {
                resourceName: 'evil.example.com/x?',
                apiKey: 'k',
              },
            },
          } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'protectedMaterial: missing credentials',
      () => azureProtectedMaterial(context, {} as any, 'afterRequestHook'),
    ],
    [
      'protectedMaterial: refused resourceName',
      () =>
        azureProtectedMaterial(
          {
            ...context,
            response: {
              text: 'out',
              json: {
                choices: [{ message: { role: 'assistant', content: 'out' } }],
              },
            },
          },
          {
            credentials: {
              contentSafety: { resourceName: 'bad host/', apiKey: 'k' },
            },
          } as any,
          'afterRequestHook'
        ),
    ],
    [
      'pii: refused egress (customHost on a private address)',
      () =>
        azurePii(
          context,
          {
            credentials: {
              pii: { apiKey: 'k', customHost: 'http://169.254.169.254' },
            },
          } as any,
          'beforeRequestHook',
          options
        ),
    ],
    [
      'pii: managed identity disabled (token denied)',
      () =>
        azurePii(
          context,
          {
            credentials: {
              pii: { resourceName: 'r', azureAuthMode: 'managed' },
            },
          } as any,
          'beforeRequestHook',
          options
        ),
    ],
  ];
  test.each(cases)('%s', async (_, run) => {
    expectErroredNotPassed(await run());
  });
});

describe('bedrock plugin returns verdict false on error', () => {
  const credentials = {
    awsAccessKeyId: 'a',
    awsSecretAccessKey: 's',
    awsRegion: 'us-east-1',
  };
  const cases: Case[] = [
    [
      'refused region (PluginEgressError)',
      () =>
        bedrockHandler(
          context,
          {
            guardrailId: 'g',
            guardrailVersion: '1',
            credentials: { ...credentials, awsRegion: 'x@169.254.169.254/#' },
          } as any,
          'beforeRequestHook',
          options
        ),
    ],
    [
      'empty content',
      () =>
        bedrockHandler(
          emptyContext,
          { guardrailId: 'g', guardrailVersion: '1', credentials } as any,
          'beforeRequestHook',
          options
        ),
    ],
  ];
  test.each(cases)('%s', async (_, run) => {
    expectErroredNotPassed(await run());
  });
});

describe('other vendor plugins return verdict false on error', () => {
  const cases: Case[] = [
    [
      'mistral: refused egress',
      () =>
        mistralGuardrailHandler(
          context,
          { credentials: { apiKey: 'k' }, categories: ['pii'] } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'walledai: missing apiKey',
      () => walledaiHandler(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'walledai: refused egress',
      () =>
        walledaiHandler(
          context,
          { credentials: { apiKey: 'k' } } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'promptfoo.guard: refused egress',
      () => promptfooGuard(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'promptfoo.harm: refused egress',
      () => promptfooHarm(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'promptfoo.pii: refused egress',
      () => promptfooPii(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'f5: missing credentials',
      () => f5Scan(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'f5: refused egress',
      () =>
        f5Scan(
          context,
          { projectId: 'p', credentials: { apiKey: 'k' } } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'exa: empty content',
      () =>
        exaOnline(
          emptyContext,
          { credentials: { apiKey: 'k' } } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'pangea.textGuard: missing domain',
      () => pangeaTextGuard(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'pangea.textGuard: refused egress',
      () =>
        pangeaTextGuard(
          context,
          {
            credentials: { domain: 'aws.us.pangea.cloud', apiKey: 'k' },
          } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'pangea.pii: refused egress',
      () =>
        pangeaPii(
          context,
          {
            credentials: { domain: 'aws.us.pangea.cloud', apiKey: 'k' },
          } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'crowdstrike: missing baseUrl',
      () => crowdstrikeHandler(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'crowdstrike: refused egress',
      () =>
        crowdstrikeHandler(
          context,
          {
            credentials: { baseUrl: 'https://aidr.example', apiKey: 'k' },
          } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'javelin: missing apiKey',
      () => javelinHandler(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'javelin: refused egress',
      () =>
        javelinHandler(
          context,
          { credentials: { apiKey: 'k', domain: 'localhost:8787' } } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'acuvity: empty content',
      () =>
        acuvityScan(
          emptyContext,
          { credentials: { apiKey: 'k' } } as any,
          'beforeRequestHook'
        ),
    ],
    [
      'panw: missing apiKey',
      () => panwIntercept(context, {} as any, 'beforeRequestHook', options),
    ],
    [
      'pillar: no scanners',
      () =>
        pillarScanPrompt(context, { scanners: [] } as any, 'beforeRequestHook'),
    ],
    [
      'patronus.toxicity: wrong hook',
      () => patronusToxicity(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'patronus.pii: empty content',
      () => patronusPii(emptyContext, {} as any, 'afterRequestHook'),
    ],
    [
      'portkey.pii: no categories',
      () => portkeyPii(context, {} as any, 'beforeRequestHook', options),
    ],
    [
      'portkey.gibberish: empty content',
      () =>
        portkeyGibberish(emptyContext, {} as any, 'beforeRequestHook', options),
    ],
    [
      'qualifire.policy: no policies',
      () => qualifirePolicy(context, {} as any, 'beforeRequestHook'),
    ],
    [
      'qualifire.grounding: wrong hook',
      () => qualifireGrounding(context, {} as any, 'beforeRequestHook'),
    ],
  ];
  test.each(cases)('%s', async (_, run) => {
    quiet();
    refuseEgress();
    expectErroredNotPassed(await run());
  });
});

describe('default plugins return verdict false on error', () => {
  const cases: Case[] = [
    [
      'regexReplace: invalid pattern',
      () => regexReplace(context, { rule: '(' } as any, 'beforeRequestHook'),
    ],
    [
      'addPrefix: missing prefix',
      () => addPrefix(context, {} as any, 'beforeRequestHook'),
    ],
  ];
  test.each(cases)('%s', async (_, run) => {
    expectErroredNotPassed(await run());
  });
});

describe('HooksManager: an errored check is decided by failOnError', () => {
  async function runHook(check: { id: string; parameters: any }) {
    const manager = new HooksManager();
    // A plugin that reports an error together with `verdict: true`, as the
    // upstream Azure and Bedrock plugins did; and one that throws.
    (manager as any).plugins = {
      ...(manager as any).plugins,
      test: {
        errorButPass: async () => ({
          error: { name: 'Error', message: 'vendor down' },
          verdict: true,
          data: null,
        }),
        throws: async () => {
          throw new Error('boom');
        },
      },
    };
    const span = manager.createSpan(
      { messages: [{ role: 'user', content: 'hello' }] },
      {},
      'openai',
      false,
      [
        {
          id: 'guard-1',
          type: HookType.GUARDRAIL,
          eventType: 'beforeRequestHook',
          deny: true,
          checks: [check],
        },
      ],
      [],
      null,
      'chatComplete',
      {}
    );
    return manager.executeHooks(span.id, ['syncBeforeRequestHook'], options);
  }

  const checks: [string, (failOnError: boolean) => any][] = [
    [
      'plugin returning verdict true with an error',
      (failOnError) => ({
        id: 'test.errorButPass',
        parameters: { failOnError },
      }),
    ],
    [
      'plugin that throws',
      (failOnError) => ({ id: 'test.throws', parameters: { failOnError } }),
    ],
    [
      'azure.contentSafety with a refused egress',
      (failOnError) => ({
        id: 'azure.contentSafety',
        parameters: {
          failOnError,
          credentials: {
            contentSafety: {
              resourceName: 'r',
              apiKey: 'k',
              customHost: 'http://10.0.0.5',
            },
          },
        },
      }),
    ],
    [
      'bedrock.guard with a refused region',
      (failOnError) => ({
        id: 'bedrock.guard',
        parameters: {
          failOnError,
          guardrailId: 'g',
          guardrailVersion: '1',
          credentials: {
            awsAccessKeyId: 'a',
            awsSecretAccessKey: 's',
            awsRegion: 'x@169.254.169.254/#',
          },
        },
      }),
    ],
  ];

  describe.each(checks)('%s', (_, check) => {
    test('failOnError=true denies', async () => {
      quiet();
      const { results, shouldDeny } = await runHook(check(true));
      expect(results[0].checks[0].error).toBeTruthy();
      expect(results[0].checks[0].verdict).toBe(false);
      expect(results[0].checks[0].fail_on_error).toBe(true);
      expect(results[0].verdict).toBe(false);
      expect(shouldDeny).toBe(true);
    });

    test('failOnError=false fails open (documented behaviour)', async () => {
      quiet();
      const { results, shouldDeny } = await runHook(check(false));
      expect(results[0].checks[0].error).toBeTruthy();
      expect(results[0].checks[0].verdict).toBe(false);
      expect(results[0].verdict).toBe(true);
      expect(shouldDeny).toBe(false);
    });
  });
});
