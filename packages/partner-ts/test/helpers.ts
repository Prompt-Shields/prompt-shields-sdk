import type { FetchLike } from '../src';

export interface RecordedCall {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | undefined;
}

type Responder = (call: RecordedCall) => Response | Promise<Response>;

export function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

/**
 * A fetch mock that serves queued responses in order and records each call.
 * A queued function is called with the request and returns the response.
 */
export function mockFetch(...queue: Array<Response | Responder | Error>) {
  const calls: RecordedCall[] = [];
  const fetch: FetchLike = async (input, init) => {
    const call: RecordedCall = {
      url: input,
      method: init.method ?? 'GET',
      headers: { ...(init.headers as Record<string, string>) },
      body: init.body as string | undefined,
    };
    calls.push(call);
    const next = queue.shift();
    if (next === undefined) throw new Error(`Unexpected request: ${call.method} ${call.url}`);
    if (next instanceof Error) throw next;
    if (typeof next === 'function') return next(call);
    return next;
  };
  return { fetch, calls, remaining: () => queue.length };
}

export const ASSET_WIRE = {
  id: '019e1a2b-3c4d-7000-8000-000000000001',
  vendor: 'openai',
  model: 'gpt-4o',
  use_case_name: 'interview-screening',
  business_unit: 'HR',
  owner_email: 'jane.doe@acme.com',
  environment: 'production',
  status: 'active',
  data_classification: 'confidential',
  discovery_source: ['sdk', 'browser_extension'],
  confidence: 'verified',
  first_seen: '2026-03-01T10:00:00Z',
  last_seen: '2026-03-25T14:22:00Z',
};

export function asset(id: string, extra: Record<string, unknown> = {}) {
  return { ...ASSET_WIRE, id, ...extra };
}

export function token(accessToken = 'a.b.c', expiresIn = 3600): Response {
  return json({ access_token: accessToken, token_type: 'Bearer', expires_in: expiresIn, scope: 'registry:read' });
}

export const noSleep = async (_ms: number) => {};
