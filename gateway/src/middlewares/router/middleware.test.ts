import { psRouterMiddleware } from './middleware';

const run = (body: any, headers: Record<string, string> = {}): Promise<any> => {
  const mw = psRouterMiddleware();
  const request: any = {
    url: 'https://api.openai.com/v1/chat/completions',
    headers,
    body,
  };
  return mw.beforeRequest(request) as Promise<any>;
};

describe('psRouterMiddleware', () => {
  test('rewrites model="auto" to a concrete model', async () => {
    const req = await run({
      model: 'auto',
      messages: [{ role: 'user', content: 'hi' }],
    });
    expect(req.body.model).not.toBe('auto');
    expect(typeof req.body.model).toBe('string');
    expect(req._psRoute.requestedModel).toBe('auto');
    expect(req._psRoute.servedModel).toBe(req.body.model);
  });

  test('a draft quality hint routes an auto request to a cheap model', async () => {
    const req = await run(
      { model: 'auto', messages: [{ role: 'user', content: 'hi' }] },
      { 'X-PS-Quality': 'draft' }
    );
    expect(req._psRoute.group).toBe('cheap');
    expect(req._psRoute.reason).toContain('quality');
  });

  test('an explicit concrete model with no hints is left untouched', async () => {
    const req = await run({
      model: 'gpt-4o',
      messages: [{ role: 'user', content: 'hi' }],
    });
    expect(req.body.model).toBe('gpt-4o');
    expect(req._psRoute).toBeUndefined();
  });

  test('an explicit X-PS-Route override routes even a concrete-model request', async () => {
    const req = await run(
      { model: 'gpt-4o', messages: [{ role: 'user', content: 'hi' }] },
      { 'x-ps-route': 'cheap' }
    );
    expect(req._psRoute.group).toBe('cheap');
    expect(req._psRoute.reason).toContain('override');
  });

  test.each(['constructor', '__proto__', 'toString', 'hasOwnProperty'])(
    'X-PS-Route: %s routes to the default group like any unknown group',
    async (name) => {
      const req = await run(
        { model: 'auto', messages: [{ role: 'user', content: 'hi' }] },
        { 'X-PS-Route': name }
      );
      expect(req._psRoute.group).toBe('balanced');
      expect(req._psRoute.reason).toBe('override:unknown-group-default');
      expect(req.body.model).toBe('gpt-4o');
    }
  );

  test('never throws on a malformed body (fail-open passthrough)', async () => {
    const req = await run(undefined as any);
    expect(req).toBeDefined();
  });
});
