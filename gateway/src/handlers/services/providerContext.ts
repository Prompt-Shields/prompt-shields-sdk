// providerContext.ts

import {
  ProviderAPIConfig,
  ProviderConfigs,
  RequestHandlers,
} from '../../providers/types';
import Providers from '../../providers';
import { RequestContext } from './requestContext';
import { ANTHROPIC, AZURE_OPEN_AI } from '../../globals';
import { GatewayError } from '../../errors/GatewayError';
import { stripProxyEndpointPath } from '../proxyGuardrails';

/** The lower-cased `host[:port]` of an http(s) base URL, or '' if none. */
function proxyBaseHost(baseURL: string): string {
  try {
    const url = new URL(baseURL);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return '';
    if (url.username || url.password) return '';
    return url.host.toLowerCase();
  } catch {
    return '';
  }
}

/**
 * PS fork (security): a proxied request goes only to the origin of the
 * provider's configured base URL (or custom host), so a request path can
 * never choose the host, scheme or port the provider credentials are sent to.
 */
export function assertProxyURLOrigin(proxyURL: string, baseURL: string) {
  let target: URL;
  let base: URL;
  try {
    target = new URL(proxyURL);
    base = new URL(baseURL);
  } catch {
    throw new GatewayError('Invalid proxy path', 400);
  }
  if (
    !proxyBaseHost(baseURL) ||
    target.username ||
    target.password ||
    target.origin !== base.origin
  ) {
    throw new GatewayError('Invalid proxy path', 400);
  }
}

export class ProviderContext {
  constructor(private provider: string) {
    if (!Providers[provider]) {
      throw new GatewayError(`Provider ${provider} not found`);
    }
  }

  get providerConfig(): ProviderConfigs {
    return Providers[this.provider];
  }

  get apiConfig(): ProviderAPIConfig {
    return this.providerConfig.api;
  }

  async getHeaders(context: RequestContext): Promise<Record<string, any>> {
    return await this.apiConfig?.headers({
      c: context.honoContext,
      providerOptions: context.providerOption,
      fn: context.endpoint,
      transformedRequestBody: context.transformedRequestBody,
      transformedRequestUrl: context.requestURL,
      gatewayRequestBody: context.params,
    });
  }

  /**
   * Get the base URL for the provider. Be careful, this returns a promise.
   * @returns The base URL for the provider.
   */
  async getBaseURL(context: RequestContext): Promise<string> {
    return await this.apiConfig.getBaseURL({
      providerOptions: context.providerOption,
      fn: context.endpoint,
      c: context.honoContext,
      gatewayRequestURL: context.honoContext.req.url,
      params: context.params,
    });
  }

  getEndpointPath(context: RequestContext): string {
    return this.apiConfig.getEndpoint({
      c: context.honoContext,
      providerOptions: context.providerOption,
      fn: context.endpoint,
      gatewayRequestBodyJSON: context.params,
      gatewayRequestBody: {}, // not using anywhere.
      gatewayRequestURL: context.honoContext.req.url,
    });
  }

  getProxyPath(context: RequestContext, baseURL: string): string {
    let reqURL = new URL(context.honoContext.req.url);
    let reqPath = reqURL.pathname;
    const reqQuery = reqURL.search;
    // PS fork (security): the same cut the proxy guardrails classify on.
    reqPath = stripProxyEndpointPath(reqPath);
    // PS fork: `/v1/proxy.evil.com/x` would leave `.evil.com/x`, which
    // appended to a bare-host base URL (`https://fal.run`) changes the host
    // the provider's credentials are sent to.
    if (reqPath && !reqPath.startsWith('/')) {
      throw new GatewayError('Invalid proxy path', 400);
    }

    if (
      this.provider === AZURE_OPEN_AI &&
      reqPath.includes('.openai.azure.com')
    ) {
      // PS fork (security): upstream took the host from the request path
      // (`https:/${reqPath}`), so `/v1/proxy/evil.com/.openai.azure.com/...`
      // sent the Azure api-key or Entra token to evil.com. The full-URL form
      // is kept only when its first segment is exactly the configured host.
      const host = proxyBaseHost(baseURL);
      const firstSegment = reqPath.slice(1).split('/')[0].toLowerCase();
      if (!host || firstSegment !== host) {
        throw new GatewayError('Invalid proxy path', 400);
      }
      return `https:/${reqPath}${reqQuery}`;
    }

    if (this.apiConfig?.getProxyEndpoint) {
      return `${baseURL}${this.apiConfig.getProxyEndpoint({
        reqPath,
        reqQuery,
        providerOptions: context.providerOption,
      })}`;
    }

    let proxyPath = `${baseURL}${reqPath}${reqQuery}`;

    if (this.provider === ANTHROPIC) {
      proxyPath = proxyPath.replace('/v1/v1/', '/v1/');
    }

    return proxyPath;
  }

  async getFullURL(context: RequestContext): Promise<string> {
    const baseURL = context.customHost || (await this.getBaseURL(context));
    let url: string;
    if (context.endpoint === 'proxy') {
      url = this.getProxyPath(context, baseURL);
      // PS fork (security): the proxy path comes from the caller, so whatever
      // a builder made of it must stay on the configured upstream.
      assertProxyURLOrigin(url, baseURL);
    } else {
      const endpointPath = this.getEndpointPath(context);
      url = `${baseURL}${endpointPath}`;
    }

    return url;
  }

  get requestHandlers(): RequestHandlers {
    return this.providerConfig?.requestHandlers ?? {};
  }

  hasRequestHandler(context: RequestContext): boolean {
    return Boolean(this.requestHandlers?.[context.endpoint]);
  }

  getRequestHandler(
    context: RequestContext
  ): (() => Promise<Response>) | undefined {
    const requestHandler = this.requestHandlers?.[context.endpoint];
    if (!requestHandler) {
      return undefined;
    }

    return () =>
      requestHandler({
        c: context.honoContext,
        providerOptions: context.providerOption,
        requestURL: context.honoContext.req.url,
        requestHeaders: context.requestHeaders,
        requestBody: context.requestBody,
      });
  }
}
