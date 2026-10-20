/**
 * A fake upstream MCP server for the proxy tests: Streamable HTTP on
 * 127.0.0.1, speaking 2026-07-28 (stateless, Mcp-Method / Mcp-Name) and
 * 2025-11-25 (initialize, Mcp-Session-Id, GET stream, DELETE).
 *
 * Paths: `/mcp` answers with JSON, `/mcp-sse` with SSE, `/redirect` with a
 * 307 to `/mcp`. Every request is recorded. The `slow` tool streams a
 * progress notification, then waits for `release()` before its result.
 */
import { createServer, IncomingMessage, Server, ServerResponse } from 'http';
import { AddressInfo } from 'net';

export const UPSTREAM_TOKEN = 'upstream-secret-token';
export const SESSION_ID = 'session-abc123';

export interface RecordedRequest {
  method: string;
  path: string;
  headers: IncomingMessage['headers'];
  body: any;
}

/**
 * A test's own upstream behaviour (a hostile server): called for every
 * authenticated request; returns true when it has answered.
 */
export type FakeOverride = (ctx: {
  req: IncomingMessage;
  res: ServerResponse;
  body: any;
}) => boolean | Promise<boolean>;

export interface FakeMcpServer {
  url: (path?: string) => string;
  requests: RecordedRequest[];
  tools: any[];
  override: FakeOverride | undefined;
  release: () => void;
  close: () => Promise<void>;
}

export const defaultTools = () => [
  {
    name: 'echo',
    description: 'Echoes its arguments.',
    inputSchema: { type: 'object' },
  },
  {
    name: 'leak',
    description: 'Returns a secret.',
    inputSchema: { type: 'object' },
  },
  { name: 'fail', description: 'Fails.', inputSchema: { type: 'object' } },
  { name: 'slow', description: 'Streams.', inputSchema: { type: 'object' } },
  {
    name: 'delete_everything',
    description: 'Not on the allowlist.',
    inputSchema: { type: 'object' },
  },
  {
    name: 'bad name<script>',
    description: 'Invalid name.',
    inputSchema: { type: 'object' },
  },
  {
    name: 'echo',
    description: 'A second echo: duplicate.',
    inputSchema: { type: 'object' },
  },
];

const send = (
  res: ServerResponse,
  status: number,
  body: unknown,
  headers: Record<string, string> = {}
) => {
  res.writeHead(status, { 'content-type': 'application/json', ...headers });
  res.end(body === undefined ? undefined : JSON.stringify(body));
};

const sse = (message: unknown) =>
  `event: message\ndata: ${JSON.stringify(message)}\n\n`;

export async function startFakeMcpServer(): Promise<FakeMcpServer> {
  const requests: RecordedRequest[] = [];
  const state: { tools: any[]; override: FakeOverride | undefined } = {
    tools: defaultTools(),
    override: undefined,
  };
  let releaseSlow: () => void = () => {};
  let slowGate = new Promise<void>((r) => (releaseSlow = r));

  const handle = async (req: IncomingMessage, res: ServerResponse) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(chunk as Buffer);
    const raw = Buffer.concat(chunks).toString('utf8');
    let body: any;
    try {
      body = raw ? JSON.parse(raw) : undefined;
    } catch {
      body = raw;
    }
    const path = (req.url || '').split('?')[0];
    requests.push({
      method: req.method || '',
      path,
      headers: req.headers,
      body,
    });

    if (path === '/redirect') {
      res.writeHead(307, { location: '/mcp' });
      return res.end();
    }
    if (req.headers.authorization !== `Bearer ${UPSTREAM_TOKEN}`) {
      return send(
        res,
        401,
        { error: 'unauthorized' },
        {
          'www-authenticate':
            'Bearer resource_metadata="https://idp.example/.well-known/oauth-protected-resource"',
        }
      );
    }
    if (state.override && (await state.override({ req, res, body }))) return;
    const version = req.headers['mcp-protocol-version'];
    const legacy = version === '2025-11-25';
    const session = req.headers['mcp-session-id'];

    if (req.method === 'DELETE') return send(res, 200, undefined);
    if (req.method === 'GET') {
      if (!legacy) return send(res, 405, undefined);
      if (session !== SESSION_ID) return send(res, 404, undefined);
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(
        sse({
          jsonrpc: '2.0',
          method: 'notifications/tools/list_changed',
        })
      );
      return res.end();
    }

    const { id, method, params } = body ?? {};
    if (method === 'initialize') {
      return send(
        res,
        200,
        {
          jsonrpc: '2.0',
          id,
          result: {
            protocolVersion: '2025-11-25',
            capabilities: { tools: {} },
            serverInfo: { name: 'fake', version: '1.0.0' },
          },
        },
        { 'mcp-session-id': SESSION_ID }
      );
    }
    if (legacy && session !== SESSION_ID) return send(res, 404, undefined);
    if (!legacy && req.headers['mcp-method'] !== method)
      return send(res, 400, {
        jsonrpc: '2.0',
        id,
        error: { code: -32020, message: 'Header mismatch' },
      });
    if (typeof method === 'string' && method.startsWith('notifications/'))
      return send(res, 202, undefined);

    let result: any;
    if (method === 'tools/list') {
      result = { tools: state.tools };
    } else if (method === 'tools/call') {
      const name = params?.name;
      if (name === 'slow') {
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.write(
          sse({
            jsonrpc: '2.0',
            method: 'notifications/progress',
            params: { progressToken: 1, progress: 1 },
          })
        );
        await slowGate;
        res.write(
          sse({
            jsonrpc: '2.0',
            id,
            result: { content: [{ type: 'text', text: 'slow done' }] },
          })
        );
        return res.end();
      }
      if (name === 'huge') {
        result = { content: [{ type: 'text', text: 'x'.repeat(64 * 1024) }] };
      } else if (name === 'leak') {
        result = { content: [{ type: 'text', text: 'the key is SECRET-123' }] };
      } else if (name === 'fail') {
        result = { content: [{ type: 'text', text: 'boom' }], isError: true };
      } else {
        result = {
          content: [{ type: 'text', text: JSON.stringify(params?.arguments) }],
        };
      }
    } else {
      return send(res, 404, {
        jsonrpc: '2.0',
        id,
        error: { code: -32601, message: 'Method not found' },
      });
    }
    const message = { jsonrpc: '2.0', id, result };
    if (path === '/mcp-sse') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(
        sse({
          jsonrpc: '2.0',
          method: 'notifications/message',
          params: { level: 'info', data: 'working' },
        })
      );
      res.write(sse(message));
      return res.end();
    }
    return send(res, 200, message);
  };

  const server: Server = createServer((req, res) => {
    handle(req, res).catch(() => {
      res.statusCode = 500;
      res.end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;

  return {
    url: (path = '/mcp') => `http://127.0.0.1:${port}${path}`,
    requests,
    get tools() {
      return state.tools;
    },
    set tools(value: any[]) {
      state.tools = value;
    },
    get override() {
      return state.override;
    },
    set override(value: FakeOverride | undefined) {
      state.override = value;
    },
    release: () => {
      releaseSlow();
      slowGate = new Promise<void>((r) => (releaseSlow = r));
    },
    close: () =>
      new Promise<void>((resolve) => {
        releaseSlow();
        server.closeAllConnections?.();
        server.close(() => resolve());
      }),
  };
}
