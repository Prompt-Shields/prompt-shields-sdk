/**
 * MCP proxy (#63) through the real gateway app: the route wins over the /v1
 * catch-alls, the router and LLM telemetry leave MCP traffic alone, tool
 * calls reach the collector's /ingest/mcp, and guardrail verdicts and auth
 * failures become security events.
 */
jest.mock('jose', () => ({}));

import {
  events,
  json,
  resetHarness,
  until,
  upstream,
  upstreamCalls,
} from './harness';
import app from '../../index';
import { parseMcpServers, setMcpServers } from '../../mcp/config';
import { _resetSecurityEventsForTests } from '../../middlewares/psSecurityEvents';

const MCP_URL = 'https://mcp.example.com/mcp';
const CLIENT_TOKEN = 'regression-mcp-token-123';

const call = (
  name: string,
  args: unknown,
  headers: Record<string, string> = {}
) =>
  app.request('/v1/mcp/docs', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${CLIENT_TOKEN}`,
      'mcp-protocol-version': '2026-07-28',
      'mcp-method': 'tools/call',
      'mcp-name': name,
      ...headers,
    },
    body: JSON.stringify({
      jsonrpc: '2.0',
      id: 1,
      method: 'tools/call',
      params: { name, arguments: args },
    }),
  });

let written: string[] = [];
let writeSpy: jest.SpyInstance;

beforeEach(() => {
  resetHarness();
  _resetSecurityEventsForTests();
  process.env.PS_MCP_TOKEN = CLIENT_TOKEN;
  setMcpServers(
    parseMcpServers(
      {
        docs: {
          url: MCP_URL,
          auth: { type: 'header', header: 'x-api-key', value_env: 'DOCS_KEY' },
          tools: { allow: ['search'] },
          input_guardrails: [
            { 'default.regexMatch': { rule: '\\d{4}-\\d{4}', not: true } },
          ],
        },
      },
      { DOCS_KEY: 'docs-upstream-key' }
    )
  );
  upstream(/mcp\.example\.com/, (c) =>
    json({
      jsonrpc: '2.0',
      id: c.body.id,
      result: { content: [{ type: 'text', text: 'found 3 pages' }] },
    })
  );
  written = [];
  writeSpy = jest
    .spyOn(process.stdout, 'write')
    .mockImplementation((chunk: any) => {
      written.push(String(chunk));
      return true;
    });
  process.env.PS_SECURITY_EVENTS_LOG = 'true';
});

afterEach(() => {
  writeSpy.mockRestore();
  delete process.env.PS_SECURITY_EVENTS_LOG;
  delete process.env.PS_MCP_TOKEN;
  setMcpServers(new Map());
});

const securityEvents = async () => {
  await new Promise((r) => setImmediate(r));
  return written
    .join('')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l))
    .filter((e) => e['log.logger'] === 'ps.security');
};

test('a tool call is proxied with the server credentials and reported to /ingest/mcp only', async () => {
  const res = await call('search', { q: 'onboarding' });
  expect(res.status).toBe(200);
  expect(((await res.json()) as any).result.content[0].text).toBe(
    'found 3 pages'
  );

  expect(upstreamCalls).toHaveLength(1);
  const sent = upstreamCalls[0];
  expect(sent.url).toBe(MCP_URL);
  expect(sent.headers['x-api-key']).toBe('docs-upstream-key');
  expect(sent.headers.authorization).toBeUndefined();

  await until(() => events.length > 0, 'mcp telemetry');
  // No LLM usage event: only the MCP tool-call event reached the collector.
  expect(events).toEqual([
    expect.objectContaining({
      source: 'gateway',
      server_name: 'docs',
      tool_name: 'search',
      outcome: 'ok',
    }),
  ]);
  expect(events[0].vendor).toBeUndefined();
});

test('the MCP route is not the /v1 provider catch-all', async () => {
  const res = await app.request('/v1/mcp/unknown', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${CLIENT_TOKEN}`,
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
  });
  expect(res.status).toBe(404);
  expect(((await res.json()) as any).error.code).toBe(-32015);
  expect(upstreamCalls).toHaveLength(0);
});

test('a guardrail block on tool arguments is a security event, without the content', async () => {
  const res = await call('search', { card: '4111-1111' });
  expect(((await res.json()) as any).error.code).toBe(-32011);
  expect(upstreamCalls).toHaveLength(0);
  const verdict = (await securityEvents()).find(
    (e) => e['event.name'] === 'guardrail.verdict'
  );
  expect(verdict).toMatchObject({
    'http.route': '/v1/mcp/:server',
    'ps.guardrail.check_id': 'default.regexMatch',
    'ps.guardrail.verdict': 'deny',
    'ps.guardrail.stage': 'input',
    'ps.guardrail.deny': true,
  });
  expect(written.join('')).not.toContain('4111');
});

test('a wrong MCP token is an auth.failure event in the mcp realm', async () => {
  const res = await call('search', {}, { authorization: 'Bearer nope' });
  expect(res.status).toBe(401);
  const failure = (await securityEvents()).find(
    (e) => e['event.name'] === 'auth.failure'
  );
  expect(failure).toMatchObject({
    'ps.auth.realm': 'mcp',
    'ps.auth.reason': 'invalid_credentials',
  });
});
