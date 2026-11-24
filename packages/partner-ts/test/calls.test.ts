/**
 * Logged call payloads (client.calls), against a mocked fetch.
 */
import { NotFoundError, PartnerClient } from '../src';
import { json, mockFetch, noSleep } from './helpers';

const client = (fetch: ReturnType<typeof mockFetch>['fetch']) =>
  new PartnerClient({ apiKey: 'k', fetch, sleep: noSleep });

describe('client.calls', () => {
  it('gets one call and keeps the payload as sent', async () => {
    const m = mockFetch(
      json({
        call_id: 'call-1',
        mode: 'redacted',
        received_at: '2026-11-24T12:00:00Z',
        route: '/v1/chat/completions',
        status_code: 200,
        error: false,
        request_bytes: 120,
        response_bytes: 80,
        redactions: { email: 1, ip_address: 2 },
        request: { messages: [{ role: 'user', content: 'I am [EMAIL]', tool_call_id: 'x' }] },
        response: [{ finish_reason: 'stop' }],
      }),
    );
    const call = await client(m.fetch).calls.get('call-1');
    expect(new URL(m.calls[0].url).pathname).toBe('/partner/v1/calls/call-1');
    expect(call.callId).toBe('call-1');
    expect(call.statusCode).toBe(200);
    expect(call.requestBytes).toBe(120);
    expect(call.redactions).toEqual({ email: 1, ip_address: 2 });
    expect(call.request).toEqual({
      messages: [{ role: 'user', content: 'I am [EMAIL]', tool_call_id: 'x' }],
    });
    expect(call.response).toEqual([{ finish_reason: 'stop' }]);
  });

  it('throws NotFoundError when nothing is stored', async () => {
    const m = mockFetch(json({ type: 'about:blank', title: 'Not Found', status: 404 }, 404));
    await expect(client(m.fetch).calls.get('nope')).rejects.toBeInstanceOf(NotFoundError);
  });
});
