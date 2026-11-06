/**
 * The process's budget config and store. On Node, ./loader.ts sets both at
 * startup (PS_BUDGETS_FILE, Redis). Otherwise (Workers, tests) the first
 * request builds them from an inline PS_BUDGETS and an in-memory store.
 */
import { BudgetConfig, budgetConfigFromText } from './config';
import { budgetLimitUsd } from './metrics';
import { BudgetStore, MemoryBudgetStore } from './store';

export interface BudgetRuntime {
  config: BudgetConfig;
  store: BudgetStore;
  /** How long an unsettled reservation holds budget. */
  reservationTtlMs: number;
}

export const DEFAULT_RESERVATION_TTL_MS = 15 * 60 * 1000;

let runtime: BudgetRuntime | null | undefined;

export function reservationTtlOf(env: Record<string, string | undefined>) {
  const n = Number(env.PS_BUDGET_RESERVATION_TTL_MS);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_RESERVATION_TTL_MS;
}

export function setBudgetRuntime(next: BudgetRuntime | null | undefined) {
  runtime = next;
  for (const b of next?.config.budgets ?? [])
    budgetLimitUsd.set({ budget: b.id, window: b.window }, b.limitUsd);
}

/** The runtime, building an in-memory one from PS_BUDGETS on first use. */
export function getBudgetRuntime(
  env: Record<string, string | undefined>
): BudgetRuntime | null {
  if (runtime === undefined) {
    const config = budgetConfigFromText(
      env.PS_BUDGETS,
      env.PS_BUDGET_NAMESPACE
    );
    setBudgetRuntime(
      config
        ? {
            config,
            store: new MemoryBudgetStore(),
            reservationTtlMs: reservationTtlOf(env),
          }
        : null
    );
  }
  return runtime ?? null;
}
