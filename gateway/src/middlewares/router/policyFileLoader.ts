/**
 * Node-only loader for PS_ROUTE_POLICY_FILE (#336), kept out of
 * ./policyDoc.ts so edge bundles never import `fs`.
 *
 * At start an unreadable or invalid file is an error (the gateway refuses to
 * start rather than route without the operator's data policy). After that
 * the file is re-read on SIGHUP and, with PS_ROUTE_POLICY_RELOAD_SECONDS,
 * every N seconds; a reload that fails is logged and the last good policy
 * stays active.
 */
import {
  parseRoutePolicyText,
  RoutePolicyError,
  setLoadedRoutePolicy,
} from './policyDoc';
import { RoutePolicy } from './types';

/** A route policy is a few KB; anything larger is not one. */
export const MAX_ROUTE_POLICY_FILE_BYTES = 1024 * 1024;

async function readPolicyFile(path: string): Promise<string> {
  const { readFile, stat } = await import('fs/promises');
  let info;
  try {
    info = await stat(path);
  } catch (err) {
    throw new RoutePolicyError(
      `cannot read PS_ROUTE_POLICY_FILE ${path} (${(err as NodeJS.ErrnoException).code ?? 'error'})`
    );
  }
  if (!info.isFile() || info.size > MAX_ROUTE_POLICY_FILE_BYTES) {
    throw new RoutePolicyError(
      `PS_ROUTE_POLICY_FILE ${path} must be a file of at most ${MAX_ROUTE_POLICY_FILE_BYTES} bytes`
    );
  }
  try {
    return await readFile(path, 'utf-8');
  } catch (err) {
    throw new RoutePolicyError(
      `cannot read PS_ROUTE_POLICY_FILE ${path} (${(err as NodeJS.ErrnoException).code ?? 'error'})`
    );
  }
}

/** Reload interval in ms from PS_ROUTE_POLICY_RELOAD_SECONDS; 0 = off. */
export function reloadIntervalMs(
  env: Record<string, string | undefined> = process.env
): number {
  const raw = env.PS_ROUTE_POLICY_RELOAD_SECONDS;
  if (!raw) return 0;
  const seconds = Number(raw);
  if (!Number.isFinite(seconds) || seconds < 0)
    throw new RoutePolicyError(
      `PS_ROUTE_POLICY_RELOAD_SECONDS must be a number of seconds (0 = off), got "${raw}"`
    );
  return seconds === 0 ? 0 : Math.max(1, seconds) * 1000;
}

export interface RoutePolicyWatcher {
  /** Re-read the file now; true when a new policy became active. */
  reload(): Promise<boolean>;
  stop(): void;
}

/**
 * Load the file once (throws on any problem) and make it the active policy,
 * then return a watcher that reloads it, keeping the last good policy when a
 * reload fails.
 */
export async function loadRoutePolicyFile(
  path: string,
  options: { intervalMs?: number; sighup?: boolean } = {}
): Promise<{ policy: RoutePolicy; watcher: RoutePolicyWatcher }> {
  let lastText = await readPolicyFile(path);
  const policy = parseRoutePolicyText(lastText);
  setLoadedRoutePolicy(policy);

  // The last rejected content, so a poll logs a bad file once, not each tick.
  let badText: string | undefined;
  let busy: Promise<boolean> | undefined;
  const reloadOnce = async (): Promise<boolean> => {
    let text: string | undefined;
    try {
      text = await readPolicyFile(path);
      if (text === lastText || text === badText) return false;
      const next = parseRoutePolicyText(text);
      setLoadedRoutePolicy(next);
      lastText = text;
      badText = undefined;
      console.log(
        `[PS Router] route policy reloaded from ${path}` +
          (next.policyVersion ? ` (policy_version ${next.policyVersion})` : '')
      );
      return true;
    } catch (err) {
      badText = text;
      console.error(
        `[PS Router] route policy reload failed, keeping the last good policy: ${(err as Error).message}`
      );
      return false;
    }
  };
  const reload = () => {
    busy ??= reloadOnce().finally(() => {
      busy = undefined;
    });
    return busy;
  };

  let timer: ReturnType<typeof setInterval> | undefined;
  if (options.intervalMs && options.intervalMs > 0) {
    timer = setInterval(() => void reload(), options.intervalMs);
    timer.unref?.();
  }
  const onSighup = () => void reload();
  const sighup = options.sighup && process.platform !== 'win32';
  if (sighup) process.on('SIGHUP', onSighup);

  return {
    policy,
    watcher: {
      reload,
      stop() {
        if (timer) clearInterval(timer);
        if (sighup) process.off('SIGHUP', onSighup);
      },
    },
  };
}

/**
 * Node startup: load PS_ROUTE_POLICY_FILE when set and watch it. Returns
 * undefined when the variable is unset. Throws RoutePolicyError on an
 * unreadable or invalid file.
 */
export async function loadRoutePolicyFromEnv(
  env: Record<string, string | undefined> = process.env
): Promise<{ policy: RoutePolicy; watcher: RoutePolicyWatcher } | undefined> {
  const path = env.PS_ROUTE_POLICY_FILE;
  if (!path) return undefined;
  return loadRoutePolicyFile(path, {
    intervalMs: reloadIntervalMs(env),
    sighup: true,
  });
}
