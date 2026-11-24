/**
 * The agent trace read API (client.traces), against a mocked fetch.
 */
import { PartnerClient } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

const query = (url: string) => Object.fromEntries(new URL(url).searchParams);

const TRACE = '4bf92f3577b34da6a3ce929d0e0e4736';

const summary = (n: number) => ({
  trace_id: `${TRACE.slice(0, 31)}${n}`,
  root_name: 'invoke_agent support',
  root_service_name: 'support-agent',
  start_time: '2026-11-24T12:00:00Z',
  end_time: '2026-11-24T12:00:02Z',
  duration_ms: 2000,
  span_count: 10,
  error_count: 1,
  status: 'error',
  tokens_in: 612,
  tokens_out: 72,
  session_id: 'sess-1',
  user_id_hash: null,
});

describe('client.traces', () => {
  it('lists a page with filters', async () => {
    const m = mockFetch(json({ data: [summary(1)], meta: { has_more: false, next_cursor: 'abc' } }));
    const page = await client(m.fetch).traces.listPage({
      status: 'error',
      sessionId: 'sess-1',
      since: new Date('2026-11-24T00:00:00Z'),
      limit: 20,
    });
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/traces');
    expect(query(m.calls[0].url)).toEqual({
      status: 'error',
      session_id: 'sess-1',
      since: '2026-11-24T00:00:00.000Z',
      limit: '20',
    });
    const [t] = page.data;
    expect(t.rootName).toBe('invoke_agent support');
    expect(t.spanCount).toBe(10);
    expect(t.errorCount).toBe(1);
    expect(page.meta).toEqual({ hasMore: false, nextCursor: 'abc' });
  });

  it('iterates every page by cursor', async () => {
    const m = mockFetch(
      json({ data: [summary(1), summary(2)], meta: { has_more: true, next_cursor: 'c2' } }),
      json({ data: [summary(3)], meta: { has_more: false, next_cursor: 'c3' } }),
    );
    const all = await client(m.fetch).traces.list({ limit: 2 });
    expect(all.map((t) => t.traceId.slice(-1))).toEqual(['1', '2', '3']);
    expect(query(m.calls[1].url)).toEqual({ limit: '2', cursor: 'c2' });
  });

  it('gets one trace with its spans', async () => {
    const m = mockFetch(
      json({
        ...summary(6),
        trace_id: TRACE,
        spans: [
          {
            span_id: '00f067aa0ba902b7',
            parent_span_id: null,
            name: 'invoke_agent support',
            kind: 'internal',
            status: 'unset',
            error_type: null,
            start_time: '2026-11-24T12:00:00Z',
            end_time: '2026-11-24T12:00:02Z',
            duration_ms: 2000,
            service_name: 'support-agent',
            environment: 'prod',
            operation: 'invoke_agent',
            provider: null,
            request_model: null,
            response_model: null,
            tool_name: null,
            tokens_in: null,
            tokens_out: null,
            session_id: 'sess-1',
            user_id_hash: null,
          },
        ],
      }),
    );
    const trace = await client(m.fetch).traces.get(TRACE);
    expect(new URL(m.calls[0].url).pathname).toBe(`/partner/v1/traces/${TRACE}`);
    expect(trace.traceId).toBe(TRACE);
    expect(trace.spans[0].spanId).toBe('00f067aa0ba902b7');
    expect(trace.spans[0].parentSpanId).toBeNull();
    expect(trace.spans[0].serviceName).toBe('support-agent');
  });

  it('refuses an empty trace id', () => {
    const m = mockFetch();
    expect(() => client(m.fetch).traces.get('')).toThrow('traceId');
  });
});
