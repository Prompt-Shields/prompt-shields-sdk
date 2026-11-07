/**
 * Regression tests (#377): a custom provider host is resolved before the
 * call. A public name that resolves to a link-local / cloud metadata or
 * loopback address is refused without an upstream call; private addresses
 * and hosts listed in TRUSTED_CUSTOM_HOSTS keep working.
 */
jest.mock('jose', () => ({}));

import { json, post, resetHarness, upstream, upstreamCalls } from './harness';
import app from '../../index';
import { Environment } from '../../utils/env';
import { setEgressResolverForTests } from '../../utils/egressGuard';

const env = Environment() as Record<string, any>;
const originalTrusted = env.TRUSTED_CUSTOM_HOSTS;

const resolveTo = (address: string) =>
  jest.fn(async (_host: string) => [
    { address, family: address.includes(':') ? 6 : 4 },
  ]);

const chatOk = () =>
  json({
    id: 'c',
    object: 'chat.completion',
    created: 1,
    model: 'm',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content: 'hi' },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  });

const send = () =>
  post(
    app,
    '/v1/chat/completions',
    { model: 'm', messages: [{ role: 'user', content: 'hi' }] },
    {
      'x-portkey-provider': 'openai',
      authorization: 'Bearer k',
      'x-portkey-custom-host': 'https://llm.example.com/v1',
    }
  );

beforeEach(() => {
  resetHarness();
  upstream(/llm\.example\.com\/v1\/chat\/completions$/, chatOk);
});
afterEach(() => {
  setEgressResolverForTests(null);
  env.TRUSTED_CUSTOM_HOSTS = originalTrusted;
});

test.each([
  '169.254.169.254',
  'fd00:ec2::254',
  '::ffff:a9fe:a9fe',
  '127.0.0.1',
])(
  'a custom host resolving to %s is refused before any upstream call',
  async (address) => {
    setEgressResolverForTests(resolveTo(address));
    const res = await send();
    expect(res.status).toBe(400);
    expect(upstreamCalls).toHaveLength(0);
  }
);

test('a custom host resolving to a private address is served', async () => {
  setEgressResolverForTests(resolveTo('10.0.0.7'));
  const res = await send();
  expect(res.status).toBe(200);
  expect(upstreamCalls).toHaveLength(1);
});

test('a custom host listed in TRUSTED_CUSTOM_HOSTS is not resolved', async () => {
  env.TRUSTED_CUSTOM_HOSTS = 'llm.example.com';
  const resolver = resolveTo('169.254.169.254');
  setEgressResolverForTests(resolver);
  const res = await send();
  expect(res.status).toBe(200);
  expect(resolver.mock.calls.map((c: any[]) => c[0])).not.toContain(
    'llm.example.com'
  );
});
