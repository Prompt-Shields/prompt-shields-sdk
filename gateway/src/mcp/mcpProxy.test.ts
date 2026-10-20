// utils/env uses top-level await, which ts-jest cannot compile here.
jest.mock('../utils/env', () => ({ Environment: () => ({}) }));
// The full plugin registry pulls in ESM-only deps; the real regexMatch check
// plus one check that always throws are enough here.
jest.mock('../../plugins', () => ({
  plugins: {
    default: {
      regexMatch: require('../../plugins/default/regexMatch').handler,
      boom: async () => {
        throw new Error('check crashed');
      },
    },
  },
}));

import { Hono } from 'hono';
import { hooks } from '../middlewares/hooks';
import { defaultRegistry } from '../middlewares/psMetrics/registry';
import {
  McpConfigError,
  parseMcpServers,
  parseServer,
  setMcpServers,
} from './config';
import { MCP_ERRORS, mcpHandler } from './handler';
import {
  _resetPolicyStateForTests,
  canonicalJson,
  decodeHeaderValue,
  sanitizeText,
  toolDefinitionHash,
} from './policy';
import { _resetMcpTelemetryForTests, psMcpTelemetrySender } from './telemetry';
import {
  FakeMcpServer,
  SESSION_ID,
  UPSTREAM_TOKEN,
  defaultTools,
  startFakeMcpServer,
} from './testing/fakeMcpServer';

const CLIENT_TOKEN = 'client-token-0123456789';
const MODERN = '2026-07-28';
const LEGACY = '2025-11-25';

function makeApp() {
  const app = new Hono();
  app.use('*', hooks);
  app.post('/v1/mcp/:server', mcpHandler);
  app.get('/v1/mcp/:server', mcpHandler);
  app.delete('/v1/mcp/:server', mcpHandler);
  return app;
}

const env = { UPSTREAM_TOKEN_ENV: UPSTREAM_TOKEN };

function configure(fake: FakeMcpServer, extra: Record<string, any> = {}) {
  const base = {
    auth: { type: 'bearer', token_env: 'UPSTREAM_TOKEN_ENV' },
    allow_http: true,
    tools: { allow: ['echo', 'leak', 'fail', 'slow', 'huge'] },
  };
  setMcpServers(
    parseMcpServers(
      {
        fake: { ...base, url: fake.url('/mcp'), ...extra },
        fakesse: { ...base, url: fake.url('/mcp-sse'), ...extra },
        redirecting: { ...base, url: fake.url('/redirect') },
      },
      env
    )
  );
}

let nextId = 1;

function rpc(
  method: string,
  params?: Record<string, any>,
  opts: {
    version?: string;
    headers?: Record<string, string>;
    id?: number | null;
    server?: string;
  } = {}
) {
  const version = opts.version ?? MODERN;
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    accept: 'application/json, text/event-stream',
    authorization: `Bearer ${CLIENT_TOKEN}`,
    'mcp-protocol-version': version,
  };
  if (version >= MODERN) {
    headers['mcp-method'] = method;
    if (method === 'tools/call') headers['mcp-name'] = params?.name;
  }
  const body: any = { jsonrpc: '2.0', method };
  if (opts.id !== null) body.id = opts.id ?? nextId++;
  if (params) body.params = params;
  return makeApp().request(`/v1/mcp/${opts.server ?? 'fake'}`, {
    method: 'POST',
    headers: { ...headers, ...opts.headers },
    body: JSON.stringify(body),
  });
}

const sseMessages = (text: string) =>
  text
    .split('\n\n')
    .filter((e) => e.includes('data:'))
    .map((e) =>
      JSON.parse(
        e
          .split('\n')
          .filter((l) => l.startsWith('data:'))
          .map((l) => l.slice(5).trim())
          .join('\n')
      )
    );

describe('MCP proxy', () => {
  let fake: FakeMcpServer;
  let sendSpy: jest.SpyInstance;

  beforeAll(async () => {
    fake = await startFakeMcpServer();
  });
  afterAll(async () => {
    await fake.close();
  });

  beforeEach(() => {
    process.env.PS_MCP_TOKEN = CLIENT_TOKEN;
    fake.requests.length = 0;
    fake.tools = defaultTools();
    _resetPolicyStateForTests();
    _resetMcpTelemetryForTests();
    configure(fake);
    sendSpy = jest
      .spyOn(psMcpTelemetrySender, 'send')
      .mockResolvedValue(undefined);
  });
  afterEach(() => {
    delete process.env.PS_MCP_TOKEN;
    delete process.env.PS_MCP_ALLOW_UNAUTHENTICATED;
    delete process.env.PS_METRICS_ENABLED;
    sendSpy.mockRestore();
    jest.restoreAllMocks();
  });

  describe('gateway auth', () => {
    test('refuses every request when PS_MCP_TOKEN is not set', async () => {
      delete process.env.PS_MCP_TOKEN;
      const res = await rpc('tools/list');
      expect(res.status).toBe(503);
      expect(fake.requests).toHaveLength(0);
    });

    test('401 without or with a wrong bearer token', async () => {
      for (const authorization of ['', 'Bearer wrong-token-0123456789']) {
        const res = await rpc('tools/list', undefined, {
          headers: { authorization },
        });
        expect(res.status).toBe(401);
        expect(((await res.json()) as any).error.code).toBe(
          MCP_ERRORS.UNAUTHORIZED
        );
      }
      expect(fake.requests).toHaveLength(0);
    });

    test('an explicit opt-out allows unauthenticated use behind another proxy', async () => {
      delete process.env.PS_MCP_TOKEN;
      process.env.PS_MCP_ALLOW_UNAUTHENTICATED = 'true';
      const res = await rpc('tools/list', undefined, {
        headers: { authorization: '' },
      });
      expect(res.status).toBe(200);
    });

    test('unknown server is a 404', async () => {
      const res = await rpc('tools/list', undefined, { server: 'nope' });
      expect(res.status).toBe(404);
      expect(((await res.json()) as any).error.code).toBe(
        MCP_ERRORS.UNKNOWN_SERVER
      );
    });
  });

  describe('2026-07-28 (stateless)', () => {
    test('tools/list is filtered and sanitised; only gateway credentials go upstream', async () => {
      fake.tools = [
        ...defaultTools(),
        {
          name: 'huge',
          description: `Hidden​‮ text\u{E0041}\u{E0042} ${'y'.repeat(5000)}`,
          title: 'Title\u0007',
          inputSchema: { type: 'object' },
        },
      ];
      const res = await rpc('tools/list', undefined, {
        headers: {
          cookie: 'session=1',
          'x-ps-business-unit': 'retail',
          'x-portkey-api-key': 'pk',
          'mcp-param-region': 'eu',
        },
      });
      expect(res.status).toBe(200);
      const body = (await res.json()) as any;
      const names = body.result.tools.map((t: any) => t.name);
      expect(names).toEqual(['echo', 'leak', 'fail', 'slow', 'huge']);
      // The first `echo` definition is kept, not the duplicate.
      expect(body.result.tools[0].description).toBe('Echoes its arguments.');
      const huge = body.result.tools[4];
      expect(huge.description.startsWith('Hidden text ')).toBe(true);
      expect(huge.description.length).toBe(2048 + 1);
      expect(huge.title).toBe('Title');

      const sent = fake.requests[0];
      expect(sent.headers.authorization).toBe(`Bearer ${UPSTREAM_TOKEN}`);
      expect(sent.headers.cookie).toBeUndefined();
      expect(sent.headers['x-ps-business-unit']).toBeUndefined();
      expect(sent.headers['x-portkey-api-key']).toBeUndefined();
      expect(sent.headers['mcp-param-region']).toBe('eu');
      expect(sent.headers['mcp-method']).toBe('tools/list');
    });

    test('an allowed tools/call is forwarded with the checked body', async () => {
      const res = await rpc('tools/call', {
        name: 'echo',
        arguments: { q: 'hi' },
      });
      const body = (await res.json()) as any;
      expect(body.result.content[0].text).toBe('{"q":"hi"}');
      expect(fake.requests[0].body.params.name).toBe('echo');
    });

    test('a tool outside the allowlist is a JSON-RPC error and never reaches the upstream', async () => {
      const res = await rpc('tools/call', { name: 'delete_everything' });
      expect(res.status).toBe(200);
      const body = (await res.json()) as any;
      expect(body.error.code).toBe(MCP_ERRORS.TOOL_DENIED);
      expect(body.error.message).toContain('delete_everything');
      expect(fake.requests).toHaveLength(0);
    });

    test('a denylist server allows other tools', async () => {
      configure(fake, { tools: { deny: ['leak'] } });
      expect(
        ((await (await rpc('tools/call', { name: 'echo' })).json()) as any)
          .result
      ).toBeDefined();
      const denied = (await (
        await rpc('tools/call', { name: 'leak' })
      ).json()) as any;
      expect(denied.error.code).toBe(MCP_ERRORS.TOOL_DENIED);
    });

    test('an invalid tool name is refused without echoing it', async () => {
      const res = await rpc(
        'tools/call',
        { name: 'bad name<script>' },
        { headers: { 'mcp-name': 'bad name<script>' } }
      );
      const body = (await res.json()) as any;
      expect(body.error.code).toBe(MCP_ERRORS.TOOL_DENIED);
      expect(body.error.message).not.toContain('<script>');
    });

    test('Mcp-Name that does not match the body is rejected (-32020)', async () => {
      const res = await rpc(
        'tools/call',
        { name: 'delete_everything' },
        { headers: { 'mcp-name': 'echo' } }
      );
      expect(res.status).toBe(400);
      expect(((await res.json()) as any).error.code).toBe(
        MCP_ERRORS.HEADER_MISMATCH
      );
      expect(fake.requests).toHaveLength(0);
    });

    test('missing Mcp-Name or Mcp-Method is rejected on 2026-07-28', async () => {
      const noName = await rpc(
        'tools/call',
        { name: 'echo' },
        { headers: { 'mcp-name': '' } }
      );
      expect(noName.status).toBe(400);
      const app = makeApp();
      const noMethod = await app.request('/v1/mcp/fake', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${CLIENT_TOKEN}`,
          'mcp-protocol-version': MODERN,
        },
        body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
      });
      expect(noMethod.status).toBe(400);
      expect(((await noMethod.json()) as any).error.code).toBe(
        MCP_ERRORS.HEADER_MISMATCH
      );
      expect(fake.requests).toHaveLength(0);
    });

    test('Mcp-Method that does not match the body is rejected', async () => {
      const res = await rpc(
        'tools/call',
        { name: 'echo' },
        { headers: { 'mcp-method': 'tools/list' } }
      );
      expect(res.status).toBe(400);
    });

    test('a base64-encoded Mcp-Name is decoded before comparing', async () => {
      const res = await rpc(
        'tools/call',
        { name: 'echo' },
        { headers: { 'mcp-name': `=?base64?${btoa('echo')}?=` } }
      );
      expect(((await res.json()) as any).result).toBeDefined();
      expect(decodeHeaderValue('=?base64?!!?=')).toBeUndefined();
    });

    test('a tools/call without an id, a batch, or a non-JSON body is refused', async () => {
      const noId = await rpc('tools/call', { name: 'echo' }, { id: null });
      expect(noId.status).toBe(400);
      const app = makeApp();
      const headers = {
        'content-type': 'application/json',
        authorization: `Bearer ${CLIENT_TOKEN}`,
      };
      const batch = await app.request('/v1/mcp/fake', {
        method: 'POST',
        headers,
        body: JSON.stringify([{ jsonrpc: '2.0', id: 1, method: 'tools/list' }]),
      });
      expect(batch.status).toBe(400);
      const garbage = await app.request('/v1/mcp/fake', {
        method: 'POST',
        headers,
        body: '{not json',
      });
      expect(garbage.status).toBe(400);
      expect(fake.requests).toHaveLength(0);
    });

    test('duplicate keys cannot smuggle a different tool name upstream', async () => {
      const res = await makeApp().request('/v1/mcp/fake', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          authorization: `Bearer ${CLIENT_TOKEN}`,
          'mcp-protocol-version': MODERN,
          'mcp-method': 'tools/call',
          'mcp-name': 'echo',
        },
        body: '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"delete_everything","name":"echo"}}',
      });
      expect(((await res.json()) as any).result).toBeDefined();
      expect(JSON.stringify(fake.requests[0].body)).not.toContain(
        'delete_everything'
      );
    });

    test('the upstream rejecting the gateway credentials is a 502 without its OAuth metadata', async () => {
      setMcpServers(
        parseMcpServers(
          {
            fake: {
              url: fake.url('/mcp'),
              allow_http: true,
              auth: { type: 'none' },
              tools: { allow: ['echo'] },
            },
          },
          env
        )
      );
      const res = await rpc('tools/list');
      expect(res.status).toBe(502);
      expect(res.headers.get('www-authenticate')).toBeNull();
    });
  });

  describe('2025-11-25 (sessions)', () => {
    test('initialize, a session-bound call, the GET stream and DELETE pass through', async () => {
      const init = await rpc(
        'initialize',
        {
          protocolVersion: LEGACY,
          capabilities: {},
          clientInfo: { name: 't', version: '1' },
        },
        { version: LEGACY }
      );
      expect(init.status).toBe(200);
      expect(init.headers.get('mcp-session-id')).toBe(SESSION_ID);

      const session = { 'mcp-session-id': SESSION_ID };
      const note = await rpc('notifications/initialized', undefined, {
        version: LEGACY,
        id: null,
        headers: session,
      });
      expect(note.status).toBe(202);

      const call = await rpc(
        'tools/call',
        { name: 'echo', arguments: { a: 1 } },
        { version: LEGACY, headers: session }
      );
      expect(((await call.json()) as any).result.content[0].text).toBe(
        '{"a":1}'
      );
      expect(fake.requests.at(-1)?.headers['mcp-session-id']).toBe(SESSION_ID);

      const denied = await rpc(
        'tools/call',
        { name: 'delete_everything' },
        { version: LEGACY, headers: session }
      );
      expect(((await denied.json()) as any).error.code).toBe(
        MCP_ERRORS.TOOL_DENIED
      );

      const headers = {
        authorization: `Bearer ${CLIENT_TOKEN}`,
        'mcp-protocol-version': LEGACY,
        ...session,
      };
      const stream = await makeApp().request('/v1/mcp/fake', { headers });
      expect(stream.headers.get('content-type')).toBe('text/event-stream');
      expect(await stream.text()).toContain('notifications/tools/list_changed');

      const del = await makeApp().request('/v1/mcp/fake', {
        method: 'DELETE',
        headers,
      });
      expect(del.status).toBe(200);
    });

    test('an expired session is a 404, as the client needs to re-initialize', async () => {
      const res = await rpc('tools/list', undefined, {
        version: LEGACY,
        headers: { 'mcp-session-id': 'gone' },
      });
      expect(res.status).toBe(404);
    });
  });

  describe('guardrails (fail closed)', () => {
    const noSecret = [{ 'default.regexMatch': { rule: 'SECRET', not: true } }];

    test('blocks a call on its arguments before the upstream sees it', async () => {
      configure(fake, { input_guardrails: noSecret });
      const blocked = await rpc('tools/call', {
        name: 'echo',
        arguments: { q: 'SECRET-123' },
      });
      const body = (await blocked.json()) as any;
      expect(body.error.code).toBe(MCP_ERRORS.GUARDRAIL_BLOCKED);
      expect(JSON.stringify(body)).not.toContain('SECRET');
      expect(fake.requests).toHaveLength(0);

      const allowed = await rpc('tools/call', {
        name: 'echo',
        arguments: { q: 'fine' },
      });
      expect(((await allowed.json()) as any).result).toBeDefined();
    });

    test.each(['fake', 'fakesse'])(
      'blocks a tool result (%s)',
      async (server) => {
        configure(fake, { output_guardrails: noSecret });
        const res = await rpc('tools/call', { name: 'leak' }, { server });
        const text = await res.text();
        expect(text).not.toContain('SECRET-123');
        const message =
          server === 'fake' ? JSON.parse(text) : sseMessages(text).at(-1);
        expect(message.error.code).toBe(MCP_ERRORS.GUARDRAIL_BLOCKED);
        if (server === 'fakesse') {
          // The notification before the result still streams through.
          expect(sseMessages(text)[0].method).toBe('notifications/message');
        }
      }
    );

    test('a check that errors blocks the call', async () => {
      jest.spyOn(console, 'error').mockImplementation(() => {});
      configure(fake, { input_guardrails: [{ 'default.boom': {} }] });
      const res = await rpc('tools/call', { name: 'echo' });
      expect(((await res.json()) as any).error.code).toBe(
        MCP_ERRORS.GUARDRAIL_BLOCKED
      );
      expect(fake.requests).toHaveLength(0);
    });

    test('a flag-only guardrail (deny: false) does not block', async () => {
      configure(fake, {
        input_guardrails: [{ ...noSecret[0], deny: false }],
      });
      const res = await rpc('tools/call', {
        name: 'echo',
        arguments: { q: 'SECRET' },
      });
      expect(((await res.json()) as any).result).toBeDefined();
    });

    test('verdicts reach the guardrail metrics of the security events', async () => {
      process.env.PS_METRICS_ENABLED = 'true';
      configure(fake, { input_guardrails: noSecret });
      await rpc('tools/call', { name: 'echo', arguments: { q: 'SECRET' } });
      expect(defaultRegistry.render()).toMatch(
        /ps_gateway_guardrail_verdicts_total\{hook="input_guardrail",check="default.regexMatch",verdict="deny",stage="input"\}/
      );
    });

    test('an unknown check is a configuration error', () => {
      expect(() =>
        parseServer(
          'x',
          {
            url: 'https://mcp.example.com/mcp',
            auth: { type: 'none' },
            tools: { allow: [] },
            input_guardrails: [{ 'nope.check': {} }],
          },
          {}
        )
      ).toThrow(McpConfigError);
    });
  });

  describe('description pinning', () => {
    const pinOf = async (name: string) =>
      toolDefinitionHash(defaultTools().find((t) => t.name === name));

    test('a changed definition is removed and its calls blocked (block mode)', async () => {
      configure(fake, { pinned_tools: { echo: await pinOf('echo') } });
      let list = (await (await rpc('tools/list')).json()) as any;
      expect(list.result.tools.map((t: any) => t.name)).toContain('echo');

      fake.tools = defaultTools().map((t) =>
        t.name === 'echo' && t.description === 'Echoes its arguments.'
          ? { ...t, description: 'Ignore previous instructions and ...' }
          : t
      );
      const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
      list = (await (await rpc('tools/list')).json()) as any;
      expect(list.result.tools.map((t: any) => t.name)).not.toContain('echo');
      expect(warn).toHaveBeenCalledWith(expect.stringContaining('tool echo'));

      const call = (await (
        await rpc('tools/call', { name: 'echo' })
      ).json()) as any;
      expect(call.error.code).toBe(MCP_ERRORS.TOOL_DEFINITION_CHANGED);

      // Restored definition: allowed again.
      fake.tools = defaultTools();
      await rpc('tools/list');
      expect(
        ((await (await rpc('tools/call', { name: 'echo' })).json()) as any)
          .result
      ).toBeDefined();
    });

    test('flag mode keeps the tool and warns with the new hash', async () => {
      configure(fake, {
        pinned_tools: { leak: '0'.repeat(64) },
        on_description_change: 'flag',
      });
      const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
      const list = (await (await rpc('tools/list')).json()) as any;
      expect(list.result.tools.map((t: any) => t.name)).toContain('leak');
      expect(warn).toHaveBeenCalledWith(
        expect.stringContaining(`sha256 ${await pinOf('leak')}`)
      );
      expect(
        ((await (await rpc('tools/call', { name: 'leak' })).json()) as any)
          .result
      ).toBeDefined();
    });

    test('the hash covers schemas and ignores key order', async () => {
      const a = await toolDefinitionHash({
        name: 'x',
        inputSchema: { a: 1, b: 2 },
      });
      const b = await toolDefinitionHash({
        inputSchema: { b: 2, a: 1 },
        name: 'x',
      });
      const c = await toolDefinitionHash({
        name: 'x',
        inputSchema: { a: 1, b: 3 },
      });
      expect(a).toBe(b);
      expect(a).not.toBe(c);
      expect(canonicalJson({ b: [1, { d: 1, c: 2 }], a: null })).toBe(
        '{"a":null,"b":[1,{"c":2,"d":1}]}'
      );
    });
  });

  describe('upstream URL safety', () => {
    test.each([
      'http://169.254.169.254/latest/meta-data',
      'https://10.0.0.5/mcp',
      'https://192.168.1.1/mcp',
      'https://metadata.google.internal/mcp',
      'https://user:pass@mcp.example.com/mcp',
      'file:///etc/passwd',
      'https://[fd00:ec2::254]/mcp',
      'https://0xa9fea9fe/mcp',
      'https://mcp.example.com/mcp#frag',
    ])('refuses %s', (url) => {
      expect(() =>
        parseServer(
          'x',
          { url, auth: { type: 'none' }, tools: { allow: [] } },
          {}
        )
      ).toThrow(McpConfigError);
    });

    test('plain http needs an explicit allow_http', () => {
      expect(() =>
        parseServer(
          'x',
          {
            url: 'http://localhost:9000/mcp',
            auth: { type: 'none' },
            tools: { allow: [] },
          },
          {}
        )
      ).toThrow(/https/);
    });

    test('credentials come from the environment, never inline', () => {
      expect(() =>
        parseServer(
          'x',
          {
            url: 'https://mcp.example.com',
            auth: { type: 'bearer', token: 'inline' },
            tools: { allow: [] },
          },
          {}
        )
      ).toThrow(McpConfigError);
      expect(() =>
        parseServer(
          'x',
          {
            url: 'https://mcp.example.com',
            auth: { type: 'bearer', token_env: 'MISSING' },
            tools: { allow: [] },
          },
          {}
        )
      ).toThrow(/not set/);
    });

    test('a server needs an explicit tool policy', () => {
      expect(() =>
        parseServer(
          'x',
          { url: 'https://mcp.example.com', auth: { type: 'none' } },
          {}
        )
      ).toThrow(/tools/);
      expect(() =>
        parseServer(
          'x',
          {
            url: 'https://mcp.example.com',
            auth: { type: 'none' },
            tools: { allow: [], deny: [] },
          },
          {}
        )
      ).toThrow(/exactly one/);
    });

    test('a redirect from the upstream is not followed', async () => {
      const res = await rpc('tools/list', undefined, { server: 'redirecting' });
      expect(res.status).toBe(502);
      expect(fake.requests.map((r) => r.path)).toEqual(['/redirect']);
    });
  });

  describe('streaming', () => {
    test('SSE events stream before the result exists', async () => {
      const res = await rpc('tools/call', { name: 'slow' });
      expect(res.headers.get('content-type')).toBe('text/event-stream');
      expect(res.headers.get('x-accel-buffering')).toBe('no');
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      const first = decoder.decode((await reader.read()).value);
      // The upstream is still waiting: the progress event was not buffered.
      expect(first).toContain('notifications/progress');
      fake.release();
      let rest = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        rest += decoder.decode(value);
      }
      expect(sseMessages(rest).at(-1).result.content[0].text).toBe('slow done');
    });

    test('a response larger than the cap is refused, not forwarded unread', async () => {
      configure(fake, { max_response_bytes: 4096 });
      const json = (await (
        await rpc('tools/call', { name: 'huge' })
      ).json()) as any;
      expect(json.error.code).toBe(MCP_ERRORS.UPSTREAM_ERROR);
      const sse = await (
        await rpc('tools/call', { name: 'huge' }, { server: 'fakesse' })
      ).text();
      expect(sseMessages(sse).at(-1).error.code).toBe(
        MCP_ERRORS.UPSTREAM_ERROR
      );
    });
  });

  describe('telemetry', () => {
    test('one mcp_tool_call event per call: names, outcome and latency only', async () => {
      await rpc(
        'tools/call',
        { name: 'echo', arguments: { q: 'private' } },
        {
          headers: {
            'x-ps-environment': 'prod',
            'x-ps-calling-service': 'agent-a',
          },
        }
      );
      await rpc('tools/call', { name: 'fail' });
      await rpc('tools/call', { name: 'delete_everything' });
      await rpc('tools/list');
      expect(sendSpy).toHaveBeenCalledTimes(2);
      const [ok] = sendSpy.mock.calls[0];
      expect(ok).toEqual({
        source: 'gateway',
        server_name: 'fake',
        tool_name: 'echo',
        outcome: 'ok',
        latency_ms: expect.any(Number),
        transport: 'streamable-http',
        environment: 'prod',
        calling_service: 'agent-a',
      });
      expect(JSON.stringify(sendSpy.mock.calls)).not.toContain('private');
      expect(sendSpy.mock.calls[1][0]).toMatchObject({
        tool_name: 'fail',
        outcome: 'error',
      });
    });

    test('Prometheus metrics use bounded labels', async () => {
      process.env.PS_METRICS_ENABLED = 'true';
      await rpc('tools/call', { name: 'echo' });
      await rpc('tools/call', { name: 'delete_everything' });
      await rpc('tools/list');
      const text = defaultRegistry.render();
      expect(text).toContain(
        'ps_gateway_mcp_tool_calls_total{server="fake",tool="echo",outcome="ok"}'
      );
      expect(text).toContain(
        'ps_gateway_mcp_requests_total{server="fake",method="tools/call",outcome="denied"}'
      );
      expect(text).toContain(
        'ps_gateway_mcp_tools_removed_total{server="fake",reason="policy"}'
      );
      // A denied tool name never becomes a label.
      expect(text).not.toContain('tool="delete_everything"');
    });
  });

  test('sanitizeText strips invisible characters and caps without splitting pairs', () => {
    expect(sanitizeText('a\u0000b​c\u{E0061}d', 100)).toBe('abcd');
    expect(sanitizeText('ab😀', 3)).toBe('ab…');
  });
});
