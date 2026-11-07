/**
 * Run by egressGuard.test.ts in a plain Node process (jest runs tests in a
 * separate realm, where fetch's default dispatcher cannot be reached):
 * real global fetch with `withEgressGuard`, against a local server.
 */
import http from 'http';
import { AddressInfo } from 'net';
import {
  initEgressGuard,
  isEgressRefused,
  setEgressResolverForTests,
  withEgressGuard,
} from './egressGuard';

async function main() {
  await initEgressGuard();
  const server = http.createServer((req, res) =>
    res.end(`ok ${String(req.headers.host).split(':')[0]}`)
  );
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const port = (server.address() as AddressInfo).port;
  const resolveTo = (address: string) => async () => [
    { address, family: address.includes(':') ? 6 : 4 },
  ];
  const attempt = async (
    host: string,
    address: string,
    policy: 'metadata' | 'local'
  ) => {
    setEgressResolverForTests(resolveTo(address));
    const url = `http://${host}:${port}/`;
    try {
      const res = await fetch(url, await withEgressGuard(url, {}, policy));
      return await res.text();
    } catch (err) {
      return isEgressRefused(err) ? 'refused' : `error ${String(err)}`;
    }
  };
  const result = {
    // The name does not exist: only the checked lookup can reach the server.
    pinned: await attempt('pinned.invalid', '127.0.0.1', 'metadata'),
    metadata: await attempt('meta.invalid', '169.254.169.254', 'metadata'),
    loopbackLocal: await attempt('loop.invalid', '127.0.0.1', 'local'),
    literal: await attempt('169.254.169.254', '127.0.0.1', 'metadata'),
  };
  server.close();
  process.stdout.write(JSON.stringify(result) + '\n');
}

main().catch((err) => {
  process.stderr.write(String(err));
  process.exit(1);
});
