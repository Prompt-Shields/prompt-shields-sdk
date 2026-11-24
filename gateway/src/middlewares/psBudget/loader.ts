/**
 * Node startup for budgets (kept out of ./runtime.ts so edge bundles never
 * import `fs` or ioredis): reads PS_BUDGETS or PS_BUDGETS_FILE and picks the
 * store. With PS_BUDGET_REDIS_URL (else REDIS_CONNECTION_STRING) every
 * replica shares one Redis; without, spend is counted per replica. Without a
 * local document but with the central policy pull on, the store is still
 * built, for the central policy's budgets section.
 */
import { BudgetConfig, budgetConfigFromText, budgetsRequested } from './config';
import { reservationTtlOf, setBudgetRuntime, setBudgetStore } from './runtime';
import { BudgetStore, MemoryBudgetStore, RedisBudgetStore } from './store';

export const MAX_BUDGETS_FILE_BYTES = 1024 * 1024;

async function storeFromEnv(
  env: Record<string, string | undefined>
): Promise<BudgetStore> {
  const url = env.PS_BUDGET_REDIS_URL || env.REDIS_CONNECTION_STRING;
  if (!url) {
    console.warn(
      '[PS Budget] no PS_BUDGET_REDIS_URL or REDIS_CONNECTION_STRING: ' +
        'spend is counted per gateway replica'
    );
    return new MemoryBudgetStore();
  }
  const { default: Redis } = await import('ioredis');
  const timeout = Number(env.PS_BUDGET_REDIS_TIMEOUT_MS) || 500;
  const client = new Redis(url, {
    // Fail fast: a budget check must not hold the call while Redis is
    // away (the document's on_store_error decides what happens then).
    commandTimeout: timeout,
    maxRetriesPerRequest: 1,
    enableOfflineQueue: true,
  });
  client.on('error', (err) =>
    console.warn('[PS Budget] Redis error:', err?.message)
  );
  return new RedisBudgetStore(client);
}

export async function loadBudgetsFromEnv(
  env: Record<string, string | undefined> = process.env
): Promise<{ config: BudgetConfig; store: BudgetStore } | undefined> {
  let text = env.PS_BUDGETS;
  if (!text && env.PS_BUDGETS_FILE) {
    const { readFile, stat } = await import('fs/promises');
    const path = env.PS_BUDGETS_FILE;
    try {
      const info = await stat(path);
      if (!info.isFile() || info.size > MAX_BUDGETS_FILE_BYTES)
        throw new Error('too large or not a file');
      text = await readFile(path, 'utf-8');
    } catch (err) {
      throw new Error(
        `cannot read PS_BUDGETS_FILE ${path}: ${(err as Error).message}`
      );
    }
  }
  const config = budgetConfigFromText(text, env.PS_BUDGET_NAMESPACE);
  if (!config) {
    setBudgetRuntime(null);
    // The central policy's budgets section can still bring budgets: give
    // them the configured store (Redis shared with the SDK when set).
    setBudgetStore(
      budgetsRequested(env)
        ? {
            store: await storeFromEnv(env),
            reservationTtlMs: reservationTtlOf(env),
          }
        : undefined
    );
    return undefined;
  }
  const store = await storeFromEnv(env);
  setBudgetRuntime({
    config,
    store,
    reservationTtlMs: reservationTtlOf(env),
  });
  return { config, store };
}
