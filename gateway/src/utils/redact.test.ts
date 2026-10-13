import {
  REDACTED,
  pickCredentialHeaders,
  redactSecrets,
  redactString,
} from './redact';

describe('redact', () => {
  test.each([
    ['redis://default:hunter2@cache:6379', 'hunter2'],
    ['postgres://ps_user:pw@db/x', ':pw@'],
    ['Authorization: Bearer sk-abc123def456', 'sk-abc123def456'],
    ['key sk-ant-api03-abcdefghijkl leaked', 'sk-ant-api03-abcdefghijkl'],
    ['AKIAABCDEFGHIJKLMNOP in error', 'AKIAABCDEFGHIJKLMNOP'],
    [
      '?key=AIzaSyA1234567890abcdefghijklmnopqrstuv',
      'AIzaSyA1234567890abcdefghijklmnopqrstuv',
    ],
  ])('masks %s', (input, secret) => {
    const out = redactString(input);
    expect(out).not.toContain(secret);
    expect(out).toContain(REDACTED);
  });

  test('keeps file names in stack traces readable', () => {
    const stack = 'at sendTelemetry (src/middlewares/ps-telemetry.ts:85:13)';
    expect(redactString(stack)).toBe(stack);
  });

  test('leaves ordinary text alone', () => {
    expect(redactString('model gpt-4o returned 429 from api.openai.com')).toBe(
      'model gpt-4o returned 429 from api.openai.com'
    );
  });

  test('masks sensitive keys in nested objects without mutating input', () => {
    const input = {
      provider: 'openai',
      headers: { authorization: 'x', 'x-api-key': 'y', accept: 'json' },
      nested: [{ awsSecretAccessKey: 'z', region: 'eu-north-1' }],
    };
    const out: any = redactSecrets(input);
    expect(out.provider).toBe('openai');
    expect(out.headers.authorization).toBe(REDACTED);
    expect(out.headers['x-api-key']).toBe(REDACTED);
    expect(out.headers.accept).toBe('json');
    expect(out.nested[0].awsSecretAccessKey).toBe(REDACTED);
    expect(out.nested[0].region).toBe('eu-north-1');
    expect(input.headers.authorization).toBe('x');
  });

  test('errors keep name and code but lose secrets', () => {
    const err: any = new Error('connect to redis://u:p4ss@h failed');
    err.code = 'ECONNREFUSED';
    const out: any = redactSecrets(err);
    expect(out.message).not.toContain('p4ss');
    expect(out.code).toBe('ECONNREFUSED');
  });

  test('pickCredentialHeaders keeps real provider credentials only', () => {
    expect(
      pickCredentialHeaders({
        authorization: 'Bearer sk-provider-123',
        'x-api-key': 'key-1',
        'api-key': 'Bearer undefined',
        'x-goog-api-key': '',
        'anthropic-version': '2023-06-01',
        'content-type': 'application/json',
      })
    ).toEqual({
      authorization: 'Bearer sk-provider-123',
      'x-api-key': 'key-1',
    });
  });

  test('provider credentials win over proxied client headers', () => {
    const provider = { authorization: 'Bearer sk-provider-123', accept: 'a' };
    const client = { authorization: 'Bearer attacker', accept: 'b' };
    const merged = {
      ...provider,
      ...client,
      ...pickCredentialHeaders(provider),
    };
    expect(merged.authorization).toBe('Bearer sk-provider-123');
    expect(merged.accept).toBe('b');
  });
});
