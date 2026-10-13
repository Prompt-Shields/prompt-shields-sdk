/**
 * A client that disconnects mid-stream must cancel the upstream provider
 * request, so the provider stops generating (and billing) tokens.
 */
import http from 'node:http';
import { AddressInfo } from 'node:net';
import { handleStreamingMode } from './streamHandler';
import { adaptResponse } from './adapterUtils';
import { LogObjectBuilder, LogsService } from './services/logsService';
import { observeSSE, StreamSummary } from '../middlewares/psStreamObserver';

const enc = new TextEncoder();
const dec = new TextDecoder();

const chunk = (i: number) =>
  `data: {"id":"c1","choices":[{"index":0,"delta":{"content":"t${i}"},"finish_reason":null}]}\n\n`;

/**
 * An upstream body that serves `total` chunks, one per pull. Pulls after
 * `stallAfter` chunks never resolve, like a provider that is still thinking.
 */
function upstream(total: number, stallAfter = Infinity, failAt?: number) {
  const state = { cancelled: false, reason: undefined as unknown, served: 0 };
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (state.served === failAt) {
        controller.error(new Error('upstream reset'));
        return;
      }
      if (state.served >= stallAfter) return new Promise<void>(() => {});
      if (state.served >= total) {
        controller.enqueue(enc.encode('data: [DONE]\n\n'));
        controller.close();
        return;
      }
      controller.enqueue(enc.encode(chunk(state.served++)));
    },
    cancel(reason) {
      state.cancelled = true;
      state.reason = reason;
    },
  });
  const response = new Response(body, {
    headers: { 'content-type': 'text/event-stream' },
  });
  return { response, state };
}

const stream = (response: Response) =>
  handleStreamingMode(
    response,
    'openai',
    undefined,
    'https://api.openai.com/v1/chat/completions',
    false,
    {} as any,
    'chatComplete',
    {} as any
  );

const tick = (ms = 20) => new Promise((r) => setTimeout(r, ms));

async function until(cond: () => boolean, ms = 2000) {
  const end = Date.now() + ms;
  while (!cond() && Date.now() < end) await tick(5);
  return cond();
}

describe('handleStreamingMode client cancellation', () => {
  let errorSpy: jest.SpyInstance;
  beforeEach(() => {
    errorSpy = jest.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => errorSpy.mockRestore());

  test('cancelling the client response cancels the upstream body', async () => {
    const { response, state } = upstream(100);
    const reader = stream(response).body!.getReader();
    await reader.read();
    await reader.cancel('client gone');
    expect(await until(() => state.cancelled)).toBe(true);
    expect(state.served).toBeLessThan(100);
    expect(errorSpy).not.toHaveBeenCalled();
  });

  test('cancels upstream even while waiting on a slow upstream chunk', async () => {
    const { response, state } = upstream(100, 2);
    const reader = stream(response).body!.getReader();
    await reader.read();
    await reader.read();
    await tick(50); // the handler is now blocked on upstream read()
    await reader.cancel();
    expect(await until(() => state.cancelled)).toBe(true);
  });

  test('a complete stream is forwarded unchanged and not cancelled', async () => {
    const { response, state } = upstream(5);
    const text = await stream(response).text();
    const expected = [0, 1, 2, 3, 4].map(chunk).join('') + 'data: [DONE]\n\n';
    expect(text).toBe(expected);
    expect(state.cancelled).toBe(false);
  });

  test('an upstream error still closes the client stream', async () => {
    const { response } = upstream(5, Infinity, 2);
    const text = await stream(response).text();
    expect(text).toBe(chunk(0) + chunk(1));
    expect(errorSpy).toHaveBeenCalledWith(
      'Error during stream processing:',
      'openai',
      expect.any(Error)
    );
  });

  test('through psStreamObserver: upstream cancelled, stream_cancelled once', async () => {
    const { response, state } = upstream(100);
    const summaries: StreamSummary[] = [];
    const observed = observeSSE(stream(response), (s) => summaries.push(s));
    const reader = observed.body!.getReader();
    const first = await reader.read();
    expect(dec.decode(first.value)).toBe(chunk(0));
    await reader.cancel();
    expect(await until(() => state.cancelled)).toBe(true);
    await tick(50);
    expect(summaries).toHaveLength(1);
    expect(summaries[0]).toMatchObject({ cancelled: true, truncated: false });
  });

  test('the log copy of a stream does not keep the upstream alive', async () => {
    const { response, state } = upstream(100);
    const client = stream(response);
    const store = new Map<string, any>();
    const ctx: any = {
      get: (k: string) => store.get(k),
      set: store.set.bind(store),
    };
    const builder = new LogObjectBuilder(new LogsService(ctx), {
      providerOption: {},
      requestURL: '',
      endpoint: '',
      requestBody: {},
      index: 0,
      cacheConfig: {},
    } as any);
    builder.addResponse(client, null);
    const logged: Response = (builder as any).logData.response;
    expect(logged.status).toBe(200);
    expect(logged.headers.get('content-type')).toBe('text/event-stream');

    const reader = client.body!.getReader();
    await reader.read();
    await reader.cancel();
    expect(await until(() => state.cancelled)).toBe(true);
  });

  test('non-stream log copies are still full clones', async () => {
    const res = new Response('{"ok":true}', {
      headers: { 'content-type': 'application/json' },
    });
    const builder = new LogObjectBuilder(new LogsService({} as any), {
      providerOption: {},
      requestBody: {},
      cacheConfig: {},
    } as any);
    builder.addResponse(res, null);
    expect(await (builder as any).logData.response.json()).toEqual({
      ok: true,
    });
    expect(await res.json()).toEqual({ ok: true });
  });

  test.each(['messages', 'createModelResponse'] as const)(
    'adapter (%s): client cancel cancels upstream, log copy is headers only',
    async (originalFn) => {
      const { response, state } = upstream(100);
      const requestOptions: any[] = [{ providerOptions: {} }];
      const c: any = { get: () => requestOptions };
      const adapted = await adaptResponse(
        stream(response),
        { isActive: true, originalFn } as any,
        c
      );
      expect(requestOptions[0].response.body).toBeNull();
      expect(requestOptions[0].response.headers.get('content-type')).toBe(
        'text/event-stream'
      );
      const reader = adapted.body!.getReader();
      const first = await reader.read();
      expect(first.done).toBe(false);
      await reader.cancel();
      expect(await until(() => state.cancelled)).toBe(true);
      expect(state.served).toBeLessThan(100);
      await tick(50);
      expect(errorSpy).not.toHaveBeenCalled();
    }
  );

  test('Node fetch: client cancel closes the upstream connection', async () => {
    let closedEarly = false;
    let timer: NodeJS.Timeout | undefined;
    const server = http.createServer((req, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      let i = 0;
      let done = false;
      timer = setInterval(() => {
        if (i >= 200) {
          done = true;
          clearInterval(timer);
          res.end('data: [DONE]\n\n');
          return;
        }
        res.write(chunk(i++));
      }, 10);
      res.on('close', () => {
        clearInterval(timer);
        if (!done) closedEarly = true;
      });
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
    try {
      const { port } = server.address() as AddressInfo;
      const upstreamRes = await fetch(`http://127.0.0.1:${port}/`);
      const reader = stream(upstreamRes).body!.getReader();
      await reader.read();
      await reader.cancel();
      expect(await until(() => closedEarly)).toBe(true);
    } finally {
      clearInterval(timer);
      server.closeAllConnections();
      await new Promise((r) => server.close(r));
    }
  });
});
