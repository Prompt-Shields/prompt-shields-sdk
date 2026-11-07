const mockEnv: Record<string, any> = {};
// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('./env', () => ({ Environment: () => mockEnv }));

import { guardedUpstreamFetch } from './egressFetch';
import { EgressRefusedError, setEgressResolverForTests } from './egressGuard';
import { OtlpJsonExporter } from '../middlewares/psOtel';

const resolveTo = (address: string) =>
  jest.fn(async (_host: string) => [
    { address, family: address.includes(':') ? 6 : 4 },
  ]);

afterEach(() => {
  setEgressResolverForTests(null);
  delete mockEnv.TRUSTED_CUSTOM_HOSTS;
  jest.restoreAllMocks();
});

describe('guardedUpstreamFetch (#377)', () => {
  test.each(['169.254.169.254', 'fd00:ec2::254', '64:ff9b::a9fe:a9fe'])(
    'refuses a name resolving to %s without a request',
    async (address) => {
      setEgressResolverForTests(resolveTo(address));
      const fetchSpy = jest.spyOn(globalThis, 'fetch');
      await expect(
        guardedUpstreamFetch('https://otel.example.com/v1/traces', {})
      ).rejects.toBeInstanceOf(EgressRefusedError);
      expect(fetchSpy).not.toHaveBeenCalled();
    }
  );

  test.each(['10.0.0.7', '127.0.0.1', '8.8.8.8'])(
    'keeps private, loopback and public addresses working (%s)',
    async (address) => {
      setEgressResolverForTests(resolveTo(address));
      const fetchSpy = jest
        .spyOn(globalThis, 'fetch')
        .mockResolvedValue(new Response('ok'));
      const res = await guardedUpstreamFetch('https://otel.example.com/', {});
      expect(await res.text()).toBe('ok');
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    }
  );

  test('a TRUSTED_CUSTOM_HOSTS entry is not resolved', async () => {
    mockEnv.TRUSTED_CUSTOM_HOSTS = 'otel.example.com';
    const resolver = resolveTo('169.254.169.254');
    setEgressResolverForTests(resolver);
    jest.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('ok'));
    const res = await guardedUpstreamFetch('https://otel.example.com/', {});
    expect(res.status).toBe(200);
    expect(resolver).not.toHaveBeenCalled();
  });

  test('the OTLP exporter uses it by default', async () => {
    setEgressResolverForTests(resolveTo('169.254.169.254'));
    const fetchSpy = jest.spyOn(globalThis, 'fetch');
    const exporter = new OtlpJsonExporter({
      endpoint: 'https://otel.example.com/v1/traces',
    } as any);
    expect((exporter as any).fetchFn).toBe(guardedUpstreamFetch);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});
