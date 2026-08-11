import { Context } from 'hono';
import { InMemoryLruStore } from './store';

const store = new InMemoryLruStore({
  maxEntries: parseInt(process.env.PS_CACHE_MAX_ENTRIES || '1000', 10),
});

// test-only hook
export const __resetCacheForTests = () => {
  store.clear();
};

const CACHE_STATUS = {
  HIT: 'HIT',
  SEMANTIC_HIT: 'SEMANTIC HIT',
  MISS: 'MISS',
  SEMANTIC_MISS: 'SEMANTIC MISS',
  REFRESH: 'REFRESH',
  DISABLED: 'DISABLED',
};

const getCacheKey = async (requestBody: any, url: string) => {
  const stringToHash = `${JSON.stringify(requestBody)}-${url}`;
  const myText = new TextEncoder().encode(stringToHash);
  let cacheDigest = await crypto.subtle.digest(
    {
      name: 'SHA-256',
    },
    myText
  );
  return Array.from(new Uint8Array(cacheDigest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
};

// Cache Handling
export const getFromCache = async (
  env: any,
  requestHeaders: any,
  requestBody: any,
  url: string,
  organisationId: string,
  cacheMode: string,
  cacheMaxAge: number | null
) => {
  if ('x-portkey-cache-force-refresh' in requestHeaders) {
    return [null, CACHE_STATUS.REFRESH, null];
  }
  try {
    const cacheKey = await getCacheKey(requestBody, url);
    const entry = await store.get(cacheKey);
    if (entry) return [entry.responseBody, CACHE_STATUS.HIT, cacheKey];
    return [null, CACHE_STATUS.MISS, null];
  } catch (error) {
    console.error('getFromCache error: ', error);
    return [null, CACHE_STATUS.MISS, null];
  }
};

export const putInCache = async (
  env: any,
  requestHeaders: any,
  requestBody: any,
  responseBody: any,
  url: string,
  organisationId: string,
  cacheMode: string | null,
  cacheMaxAge: number | null
) => {
  if (requestBody.stream === true) {
    // Do not cache streamed responses
    return;
  }

  const cacheKey = await getCacheKey(requestBody, url);
  await store.set(cacheKey, {
    responseBody: JSON.stringify(responseBody),
    maxAge: cacheMaxAge,
  });
};

export const memoryCache = () => {
  return async (c: Context, next: any) => {
    c.set('getFromCache', getFromCache);

    await next();

    let requestOptions = c.get('requestOptions');

    if (
      requestOptions &&
      Array.isArray(requestOptions) &&
      requestOptions.length > 0 &&
      requestOptions[0].requestParams.stream !== true
    ) {
      requestOptions = requestOptions[0];
      if (requestOptions.cacheMode === 'simple') {
        try {
          const parsed = await requestOptions.response.clone().json();
          await putInCache(
            null,
            null,
            requestOptions.transformedRequest.body,
            parsed,
            requestOptions.providerOptions.rubeusURL,
            '',
            null,
            new Date().getTime() +
              (requestOptions.cacheMaxAge || 24 * 60 * 60 * 1000)
          );
        } catch (error) {
          console.error('memoryCache write-back error (fail-open):', error);
        }
      }
    }
  };
};
