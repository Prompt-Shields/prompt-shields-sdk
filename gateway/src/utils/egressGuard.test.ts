import http from 'http';
import path from 'path';
import { spawnSync } from 'child_process';
import { AddressInfo } from 'net';
import {
  DNS_CACHE_TTL_MS,
  DNS_TIMEOUT_MS,
  EgressRefusedError,
  isEgressRefused,
  isRefusedAddress,
  checkedLookup,
  resolveChecked,
  resolveCached,
  setEgressResolverForTests,
  withEgressGuard,
} from './egressGuard';

// Link-local, cloud metadata, never-unicast, and IPv6 forms embedding them.
const METADATA = [
  '169.254.169.254',
  '169.254.0.1',
  '169.254.170.2', // ECS task metadata
  '100.100.100.200', // Alibaba Cloud metadata
  '0.0.0.0',
  '0.1.2.3',
  '255.255.255.255',
  '224.0.0.1',
  '240.0.0.1',
  'fe80::1',
  'fd00:ec2::254',
  '::',
  'ff02::1',
  '::ffff:169.254.169.254',
  '::ffff:a9fe:a9fe',
  '::a9fe:a9fe', // IPv4-compatible
  '::ffff:0:a9fe:a9fe', // SIIT
  '64:ff9b::a9fe:a9fe', // NAT64
  '64:ff9b:1::a9fe:a9fe', // local-use NAT64
  '64:ff9b:1:ffff::7f00:1',
  '2002:a9fe:a9fe::1', // 6to4
  '2001:0:4136:e378:8000:63bf:5601:5601', // Teredo, client 169.254.169.254
  'not-an-ip',
];
const LOOPBACK = [
  '127.0.0.1',
  '127.1.2.3',
  '::1',
  '::ffff:127.0.0.1',
  '64:ff9b::7f00:1',
];
const ALLOWED = [
  '8.8.8.8',
  '10.0.0.7',
  '172.16.0.1',
  '192.168.1.10',
  '100.64.0.1',
  'fd12:3456::1',
  '2606:4700::1111',
  '::ffff:10.0.0.7',
  '64:ff9b::808:808',
];

describe('isRefusedAddress', () => {
  test.each(METADATA)('refuses %s under both policies', (ip) => {
    expect(isRefusedAddress(ip, 'metadata')).toBe(true);
    expect(isRefusedAddress(ip, 'local')).toBe(true);
  });
  test.each(LOOPBACK)('refuses loopback %s only under local', (ip) => {
    expect(isRefusedAddress(ip, 'metadata')).toBe(false);
    expect(isRefusedAddress(ip, 'local')).toBe(true);
  });
  test.each(ALLOWED)('admits public and private %s', (ip) => {
    expect(isRefusedAddress(ip, 'metadata')).toBe(false);
    expect(isRefusedAddress(ip, 'local')).toBe(false);
  });
});

describe('resolveCached', () => {
  afterEach(() => {
    setEgressResolverForTests(null);
    jest.useRealTimers();
  });

  test('caches a lookup for the TTL', async () => {
    const calls: string[] = [];
    setEgressResolverForTests(async (host) => {
      calls.push(host);
      return [{ address: '8.8.8.8', family: 4 }];
    });
    const now = jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    await resolveCached('a.example.com');
    await resolveCached('A.example.com');
    expect(calls).toEqual(['a.example.com']);
    now.mockReturnValue(1_000_000 + DNS_CACHE_TTL_MS + 1);
    await resolveCached('a.example.com');
    expect(calls).toHaveLength(2);
    now.mockRestore();
  });

  test('does not cache a failed lookup', async () => {
    let n = 0;
    setEgressResolverForTests(async () => {
      if (n++ === 0) throw Object.assign(new Error('x'), { code: 'EAI_AGAIN' });
      return [{ address: '8.8.8.8', family: 4 }];
    });
    await expect(resolveCached('b.example.com')).rejects.toThrow();
    await expect(resolveCached('b.example.com')).resolves.toHaveLength(1);
  });

  test('bounds a slow lookup', async () => {
    jest.useFakeTimers();
    setEgressResolverForTests(() => new Promise(() => {}));
    const p = resolveCached('slow.example.com');
    jest.advanceTimersByTime(DNS_TIMEOUT_MS + 1);
    await expect(p).rejects.toMatchObject({ code: 'ETIMEOUT' });
  });
});

const resolveTo =
  (...addresses: string[]) =>
  async () =>
    addresses.map((address) => ({
      address,
      family: address.includes(':') ? 6 : 4,
    }));

describe('checkedLookup (the connection uses only checked addresses)', () => {
  let server: http.Server;
  let port: number;
  const seen: string[] = [];

  beforeAll(async () => {
    server = http.createServer((req, res) => {
      seen.push(String(req.headers.host));
      res.end('ok');
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
    port = (server.address() as AddressInfo).port;
  });
  afterAll(async () => {
    await new Promise((r) => server.close(r));
  });
  beforeEach(() => {
    seen.length = 0;
  });
  afterEach(() => setEgressResolverForTests(null));

  const get = (host: string, policy: 'metadata' | 'local') =>
    new Promise<string>((resolve, reject) => {
      http
        .get(
          { host, port, path: '/', lookup: checkedLookup(policy) as any },
          (res) => {
            let body = '';
            res.on('data', (d) => (body += d));
            res.on('end', () => resolve(body));
          }
        )
        .on('error', reject);
    });

  test('connects to the checked address, keeping the host name', async () => {
    // The name does not exist: the request can only arrive through the
    // checked lookup.
    setEgressResolverForTests(resolveTo('127.0.0.1'));
    expect(await get('pinned.invalid', 'metadata')).toBe('ok');
    expect(seen).toEqual([`pinned.invalid:${port}`]);
  });

  test.each([
    ['169.254.169.254'],
    ['fd00:ec2::254'],
    ['::ffff:169.254.169.254'],
    ['127.0.0.1', '169.254.169.254'],
  ])('refuses a name resolving to %s', async (...addresses) => {
    setEgressResolverForTests(resolveTo(...addresses));
    const err = await get('meta.invalid', 'metadata').catch((e) => e);
    expect(isEgressRefused(err)).toBe(true);
    expect(seen).toEqual([]);
  });

  test('local policy refuses loopback', async () => {
    setEgressResolverForTests(resolveTo('127.0.0.1'));
    const err = await get('loop.invalid', 'local').catch((e) => e);
    expect(isEgressRefused(err)).toBe(true);
    expect(seen).toEqual([]);
  });
});

describe('withEgressGuard', () => {
  afterEach(() => setEgressResolverForTests(null));

  test('refuses a metadata IP literal up front', async () => {
    await expect(
      withEgressGuard('http://169.254.169.254/latest', {}, 'metadata')
    ).rejects.toBeInstanceOf(EgressRefusedError);
    await expect(
      withEgressGuard('http://[::ffff:a9fe:a9fe]/', {}, 'metadata')
    ).rejects.toBeInstanceOf(EgressRefusedError);
    await expect(
      withEgressGuard('http://10.0.0.7/', {}, 'local')
    ).resolves.toEqual({});
  });

  test('refuses a name resolving to a metadata address', async () => {
    // Under jest fetch's dispatcher lives in another realm, so this is the
    // check-before-fetch path; the pinned path runs in the child below.
    setEgressResolverForTests(resolveTo('169.254.169.254'));
    await expect(
      withEgressGuard('https://meta.invalid/', {}, 'metadata')
    ).rejects.toBeInstanceOf(EgressRefusedError);
    await expect(resolveChecked('meta.invalid', 'metadata')).rejects.toThrow(
      EgressRefusedError
    );
  });

  test('a failed lookup is left to fetch', async () => {
    setEgressResolverForTests(async () => {
      throw Object.assign(new Error('nx'), { code: 'ENOTFOUND' });
    });
    await expect(
      withEgressGuard('https://nx.invalid/', { method: 'GET' }, 'metadata')
    ).resolves.toEqual({ method: 'GET' });
  });

  test('pins Node fetch to the checked address (real runtime)', () => {
    const run = spawnSync(
      process.execPath,
      ['--import', 'tsx', path.join(__dirname, 'egressGuard.pin.fixture.ts')],
      {
        cwd: path.join(__dirname, '..', '..'),
        encoding: 'utf8',
        timeout: 60000,
      }
    );
    expect(run.stderr).toBe('');
    expect(JSON.parse(run.stdout.trim().split('\n').pop()!)).toEqual({
      pinned: 'ok pinned.invalid',
      metadata: 'refused',
      loopbackLocal: 'refused',
      literal: 'refused',
    });
  });
});
