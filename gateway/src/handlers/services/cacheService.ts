// cacheService.ts

import { Context } from 'hono';
import { HooksService } from './hooksService';
import { endpointStrings } from '../../providers/types';
import { env } from 'hono/adapter';
import { RequestContext } from './requestContext';
import {
  computeCacheScope,
  setCacheScopeForTarget,
} from '../../middlewares/cache';
import { semanticLookup } from '../../middlewares/cache/semantic';

export interface CacheResponseObject {
  cacheResponse: Response | undefined;
  cacheStatus: string;
  cacheKey: string | undefined;
  createdAt: Date;
}

export class CacheService {
  constructor(
    private honoContext: Context,
    private hooksService: HooksService
  ) {}

  isEndpointCacheable(endpoint: endpointStrings): boolean {
    const nonCacheEndpoints = [
      'uploadFile',
      'listFiles',
      'retrieveFile',
      'deleteFile',
      'retrieveFileContent',
      'createBatch',
      'retrieveBatch',
      'cancelBatch',
      'listBatches',
      'getBatchOutput',
      'listFinetunes',
      'createFinetune',
      'retrieveFinetune',
      'cancelFinetune',
      'imageEdit',
      'cancelModelResponse',
    ];
    return !nonCacheEndpoints.includes(endpoint);
  }

  get getFromCacheFunction() {
    return this.honoContext.get('getFromCache');
  }

  get getCacheIdentifier() {
    return this.honoContext.get('cacheIdentifier');
  }

  get noCacheObject(): CacheResponseObject {
    return {
      cacheResponse: undefined,
      cacheStatus: 'DISABLED',
      cacheKey: undefined,
      createdAt: new Date(),
    };
  }

  private createResponseObject(
    cacheResponse: string,
    cacheStatus: string,
    cacheKey: string,
    createdAt: Date,
    responseStatus: number
  ): CacheResponseObject {
    return {
      cacheResponse: new Response(cacheResponse, {
        headers: { 'content-type': 'application/json' },
        status: responseStatus,
      }),
      cacheStatus,
      cacheKey,
      createdAt,
    };
  }

  private async getSemantic(
    context: RequestContext,
    cacheScope: string,
    maxAge: number | undefined,
    forceRefresh: boolean
  ): Promise<[string | null, string, string | null]> {
    try {
      const result = await semanticLookup(this.honoContext, {
        endpoint: context.endpoint,
        params: context.params,
        requestHeaders: context.requestHeaders,
        index: context.index,
        cacheScope,
        maxAge,
        forceRefresh,
      });
      return [result.responseBody ?? null, result.status, result.key ?? null];
    } catch (error) {
      // Fail open: the request goes upstream and nothing is written.
      console.error('semantic cache lookup error (fail-open):', error);
      return [null, 'SEMANTIC BYPASS', null];
    }
  }

  async getCachedResponse(
    context: RequestContext,
    headers: HeadersInit
  ): Promise<CacheResponseObject> {
    if (!this.isEndpointCacheable(context.endpoint)) {
      return this.noCacheObject;
    }

    const startTime = new Date();
    const { mode, maxAge, forceRefresh } = context.cacheConfig;

    // DISABLED (or falsy) mode must not read the cache, even though the middleware is mounted.
    if (!mode || mode === 'DISABLED') {
      return this.noCacheObject;
    }

    const mergedHeaders = {
      ...context.requestHeaders,
      ...headers,
      ...(forceRefresh ? { 'x-portkey-cache-force-refresh': 'true' } : {}),
    };

    if (!this.getFromCacheFunction) {
      return this.noCacheObject;
    }

    // PS fork: scope the key to the caller (and proxy path); see
    // computeCacheScope. memoryCache reuses this scope for the write-back.
    const cacheScope = await computeCacheScope({
      provider: context.provider,
      providerOption: context.providerOption,
      requestHeaders: context.requestHeaders,
      endpoint: context.endpoint,
      requestURL: context.requestURL,
      forwardHeaders: context.forwardHeaders,
    });
    setCacheScopeForTarget(this.honoContext, context.index, cacheScope);

    // PS fork (#33): semantic mode has its own lookup (cache/semantic.ts);
    // memoryCache writes it back.
    const [cacheResponse, cacheStatus, cacheKey] =
      mode === 'semantic'
        ? await this.getSemantic(context, cacheScope, maxAge, !!forceRefresh)
        : await this.getFromCacheFunction(
            env(context.honoContext),
            mergedHeaders,
            context.transformedRequestBody,
            context.endpoint,
            this.getCacheIdentifier,
            mode,
            maxAge,
            cacheScope
          );

    if (!cacheResponse) {
      return {
        cacheResponse: undefined,
        cacheStatus: cacheStatus || 'DISABLED',
        cacheKey: !!cacheKey ? cacheKey : undefined,
        createdAt: startTime,
      };
    }

    let responseBody: string = cacheResponse;
    let responseStatus: number = 200;

    const brhResults = this.hooksService.results?.beforeRequestHooksResult;
    if (brhResults?.length) {
      responseBody = JSON.stringify({
        ...JSON.parse(responseBody),
        hook_results: {
          before_request_hooks: brhResults,
        },
      });
      responseStatus = this.hooksService.hasFailedHooks('beforeRequest')
        ? 246
        : 200;
    }

    return this.createResponseObject(
      responseBody,
      cacheStatus,
      cacheKey,
      startTime,
      responseStatus
    );
  }
}
