import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { serve } from '@hono/node-server';
import { Hono } from 'hono';
import { stream } from 'hono/streaming';
import {
  createDrain,
  DEFAULT_SHUTDOWN_TIMEOUT_MS,
  shutdownTimeoutMs,
} from './psShutdown';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function startServer(chunks: number, intervalMs: number) {
  const app = new Hono();
  app.get('/stream', (c) =>
    stream(c, async (s) => {
      for (let i = 0; i < chunks && !s.aborted; i++) {
        await s.write(`data: ${i}\n\n`);
        await sleep(intervalMs);
      }
    })
  );
  app.get('/ok', (c) => c.text('ok'));
  const drain = createDrain();
  const server = serve({ fetch: drain.wrapFetch(app.fetch), port: 0 });
  return new Promise<{
    server: http.Server;
    drain: ReturnType<typeof createDrain>;
    port: number;
  }>((resolve) =>
    server.on('listening', () =>
      resolve({
        server: server as http.Server,
        drain,
        port: (server.address() as AddressInfo).port,
      })
    )
  );
}

/** GET a path; resolves with the body once the response ends or aborts. */
function get(port: number, path: string, onFirstChunk?: () => void) {
  return new Promise<{ status?: number; body: string; complete: boolean }>(
    (resolve, reject) => {
      const req = http.get({ port, path, agent: false }, (res) => {
        let body = '';
        let first = true;
        res.setEncoding('utf8');
        res.on('data', (d) => {
          body += d;
          if (first) {
            first = false;
            onFirstChunk?.();
          }
        });
        res.on('end', () =>
          resolve({ status: res.statusCode, body, complete: res.complete })
        );
        res.on('error', () =>
          resolve({ status: res.statusCode, body, complete: false })
        );
        res.on('aborted', () =>
          resolve({ status: res.statusCode, body, complete: false })
        );
      });
      req.on('error', reject);
    }
  );
}

describe('shutdownTimeoutMs', () => {
  it('defaults when unset or invalid, else parses', () => {
    expect(shutdownTimeoutMs(undefined)).toBe(DEFAULT_SHUTDOWN_TIMEOUT_MS);
    expect(shutdownTimeoutMs('')).toBe(DEFAULT_SHUTDOWN_TIMEOUT_MS);
    expect(shutdownTimeoutMs('soon')).toBe(DEFAULT_SHUTDOWN_TIMEOUT_MS);
    expect(shutdownTimeoutMs('-1')).toBe(DEFAULT_SHUTDOWN_TIMEOUT_MS);
    expect(shutdownTimeoutMs('1500')).toBe(1500);
    expect(shutdownTimeoutMs('0')).toBe(0);
  });
});

describe('createDrain', () => {
  it('lets an in-flight streaming response finish, then reports drained', async () => {
    const { server, drain, port } = await startServer(6, 50);
    let draining: Promise<string> | undefined;
    const response = get(port, '/stream', () => {
      draining = drain.drain(server, 5000);
    });

    const res = await response;
    expect(res.status).toBe(200);
    expect(res.complete).toBe(true);
    for (let i = 0; i < 6; i++) expect(res.body).toContain(`data: ${i}`);
    expect(draining).toBeDefined();
    await expect(draining).resolves.toBe('drained');
    expect(drain.draining).toBe(true);
  });

  it('refuses new connections while draining', async () => {
    const { server, drain, port } = await startServer(6, 50);
    let draining: Promise<string> | undefined;
    let refused: unknown;
    const response = get(port, '/stream', () => {
      draining = drain.drain(server, 5000);
      get(port, '/ok').then(
        () => (refused = false),
        (err) => (refused = err)
      );
    });
    expect((await response).complete).toBe(true);
    await draining;
    await sleep(20);
    expect(refused).toMatchObject({ code: 'ECONNREFUSED' });
  });

  it('answers 503 with Connection: close to a request that arrives while draining', async () => {
    const drain = createDrain();
    const fetch = drain.wrapFetch((_req: Request) => new Response('ok'));
    expect((await fetch(new Request('http://x/ok'))).status).toBe(200);

    // A server whose close() has not completed yet: still draining.
    let closed: (() => void) | undefined;
    const draining = drain.drain(
      { close: (cb: () => void) => (closed = cb) } as any,
      60_000
    );
    const res = await fetch(new Request('http://x/ok'));
    expect(res.status).toBe(503);
    expect(res.headers.get('connection')).toBe('close');
    expect(res.headers.get('retry-after')).toBe('1');
    closed!();
    await expect(draining).resolves.toBe('drained');
  });

  it('closes connections still open at the deadline', async () => {
    // 100 chunks x 50 ms: far longer than the 200 ms deadline.
    const { server, drain, port } = await startServer(100, 50);
    let draining: Promise<string> | undefined;
    const started = Date.now();
    const response = get(port, '/stream', () => {
      draining = drain.drain(server, 200);
    });
    const res = await response;
    expect(res.complete).toBe(false);
    await expect(draining).resolves.toBe('timeout');
    expect(Date.now() - started).toBeLessThan(2000);
  });
});
