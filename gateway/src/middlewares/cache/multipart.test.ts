import { getFromCache, putInCache, __resetCacheForTests } from './index';

const maxAge = () => Date.now() + 10000;
const headers = { authorization: 'Bearer sk-same' };
const endpoint = 'createTranscription';
const scope = 'scope-a';

const audioForm = (bytes: number[]) => {
  const form = new FormData();
  form.append('model', 'whisper-1');
  form.append(
    'file',
    new Blob([new Uint8Array(bytes)], { type: 'audio/mpeg' }),
    'a.mp3'
  );
  return form;
};

describe('cache: multipart and binary bodies', () => {
  beforeEach(() => __resetCacheForTests());

  it('different audio files never share a cached response', async () => {
    await putInCache(
      {},
      headers,
      audioForm([1, 2, 3]),
      { text: 'first file' },
      endpoint,
      '',
      'simple',
      maxAge(),
      scope
    );
    const [resp, status, key] = await getFromCache(
      {},
      headers,
      audioForm([9, 9, 9]),
      endpoint,
      '',
      'simple',
      maxAge(),
      scope
    );
    expect(resp).toBeNull();
    expect(status).not.toBe('HIT');
    expect(key).toBeNull();
  });

  it.each([
    ['FormData', () => audioForm([1])],
    ['ArrayBuffer', () => new Uint8Array([1, 2]).buffer],
    ['Uint8Array', () => new Uint8Array([1, 2])],
    ['Blob', () => new Blob(['x'])],
    ['ReadableStream', () => new ReadableStream()],
  ])('bypasses the cache for a %s body', async (_name, makeBody) => {
    await putInCache(
      {},
      headers,
      makeBody(),
      { a: 1 },
      endpoint,
      '',
      'simple',
      maxAge(),
      scope
    );
    const [resp, status] = await getFromCache(
      {},
      headers,
      makeBody(),
      endpoint,
      '',
      'simple',
      maxAge(),
      scope
    );
    expect(resp).toBeNull();
    expect(status).toBe('DISABLED');
  });

  it('still caches JSON bodies', async () => {
    const body = { model: 'm', input: 'hi' };
    await putInCache(
      {},
      headers,
      body,
      { a: 1 },
      'embed',
      '',
      'simple',
      maxAge(),
      scope
    );
    const [, status] = await getFromCache(
      {},
      headers,
      body,
      'embed',
      '',
      'simple',
      maxAge(),
      scope
    );
    expect(status).toBe('HIT');
  });
});
