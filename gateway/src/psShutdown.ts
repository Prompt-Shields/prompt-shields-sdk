/**
 * Prompt Shields: graceful drain for the Node server (Kubernetes rollouts).
 *
 * On SIGTERM the server stops accepting connections and waits for requests
 * already in flight, streamed completions included, before the telemetry
 * flush and exit. The wait is bounded by PS_SHUTDOWN_TIMEOUT_MS (default
 * 25000, under Kubernetes' default 30 s terminationGracePeriodSeconds once
 * the telemetry flush is added); connections still open then are closed.
 *
 * While draining, a request that still arrives on an open keep-alive
 * connection gets 503 with `Connection: close`, so the client retries on
 * another replica instead of starting work this one cannot finish.
 *
 * Node-only (start-server.ts); the Workers build has no server to drain.
 */
export const DEFAULT_SHUTDOWN_TIMEOUT_MS = 25_000;

/** PS_SHUTDOWN_TIMEOUT_MS, or the default when unset or not a number >= 0. */
export function shutdownTimeoutMs(
  raw: string | undefined = process.env.PS_SHUTDOWN_TIMEOUT_MS
): number {
  if (raw === undefined || raw.trim() === '')
    return DEFAULT_SHUTDOWN_TIMEOUT_MS;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0
    ? value
    : DEFAULT_SHUTDOWN_TIMEOUT_MS;
}

type FetchHandler = (
  req: Request,
  ...rest: any[]
) => Response | Promise<Response>;

// What drain() needs from the server. Typed structurally because serve()
// may also return an http2 server, which lacks the closeXConnections calls.
type DrainableServer = {
  close(callback?: (err?: Error) => void): unknown;
  closeAllConnections?: () => void;
  closeIdleConnections?: () => void;
};

export type DrainResult = 'drained' | 'timeout';

export function createDrain() {
  let draining = false;

  return {
    get draining() {
      return draining;
    },

    /** Wraps the app's fetch: 503 with Connection: close while draining. */
    wrapFetch<F extends FetchHandler>(fetch: F): F {
      return ((req: Request, ...rest: any[]) => {
        if (!draining) return fetch(req, ...rest);
        return new Response(
          JSON.stringify({
            status: 'failure',
            message: 'Gateway is shutting down; retry the request.',
          }),
          {
            status: 503,
            headers: {
              'content-type': 'application/json',
              connection: 'close',
              'retry-after': '1',
            },
          }
        );
      }) as F;
    },

    /**
     * Stop accepting connections and wait for in-flight requests, at most
     * `timeoutMs`; then close whatever is still open.
     */
    drain(server: DrainableServer, timeoutMs: number): Promise<DrainResult> {
      draining = true;
      return new Promise<DrainResult>((resolve) => {
        const timer = setTimeout(() => {
          server.closeAllConnections?.();
          resolve('timeout');
        }, timeoutMs);
        server.close(() => {
          clearTimeout(timer);
          resolve('drained');
        });
        // Node 19+ also does this in close(); idle keep-alive sockets would
        // otherwise hold close() open until they time out.
        server.closeIdleConnections?.();
      });
    },
  };
}
