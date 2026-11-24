/**
 * The process's budget config and store. On Node, ./loader.ts sets both at
 * startup (PS_BUDGETS_FILE, Redis). Otherwise (Workers, tests) the first
 * request builds them from an inline PS_BUDGETS and an in-memory store.
 *
 * The central policy's `budgets` section (pulled from the collector, see
 * ../policyPull/state.ts), when the active bundle has one, replaces the local
 * document's budgets and on_store_error; the namespace and the store stay
 * the gateway's own (PS_BUDGET_NAMESPACE, else the local document's, else
 * "default"). A section with no budgets turns budgets off.
 */
import { centralBudgets } from '../policyPull/state';
import {
  Budget,
  BudgetConfig,
  budgetConfigFromText,
  CentralBudgets,
} from './config';
import {
  budgetLimitTokens,
  budgetLimitUsd,
  budgetThresholdCrossingsTotal,
} from './metrics';
import { BudgetStore, MemoryBudgetStore } from './store';

export interface BudgetRuntime {
  config: BudgetConfig;
  store: BudgetStore;
  /** How long an unsettled reservation holds budget. */
  reservationTtlMs: number;
}

export const DEFAULT_RESERVATION_TTL_MS = 15 * 60 * 1000;

let runtime: BudgetRuntime | null | undefined;
/** The store when there is no local document (central budgets only). */
let sharedStore: { store: BudgetStore; reservationTtlMs: number } | undefined;
let central:
  | { from: CentralBudgets; runtime: BudgetRuntime | null }
  | undefined;

export function reservationTtlOf(env: Record<string, string | undefined>) {
  const n = Number(env.PS_BUDGET_RESERVATION_TTL_MS);
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_RESERVATION_TTL_MS;
}

function exportLimits(budgets: Budget[]) {
  for (const b of budgets) {
    const labels = { budget: b.id, window: b.window };
    if (b.unit === 'tokens') budgetLimitTokens.set(labels, b.limit);
    else budgetLimitUsd.set(labels, b.limit);
    // Series at 0 from the start, so increase() sees the first crossing.
    for (const threshold of ['warn', 'exceeded'])
      budgetThresholdCrossingsTotal.inc({ ...labels, threshold }, 0);
  }
}

export function setBudgetRuntime(next: BudgetRuntime | null | undefined) {
  runtime = next;
  central = undefined;
  exportLimits(next?.config.budgets ?? []);
}

/**
 * The store central budgets use when there is no local document (the
 * loader sets it at startup when the policy pull is on).
 */
export function setBudgetStore(
  next: { store: BudgetStore; reservationTtlMs: number } | undefined
) {
  sharedStore = next;
  central = undefined;
}

function centralRuntime(
  section: CentralBudgets,
  env: Record<string, string | undefined>
): BudgetRuntime | null {
  if (central?.from === section) return central.runtime;
  let next: BudgetRuntime | null = null;
  if (section.budgets.length) {
    const base =
      runtime ??
      sharedStore ??
      (sharedStore = {
        store: new MemoryBudgetStore(),
        reservationTtlMs: reservationTtlOf(env),
      });
    next = {
      config: {
        namespace:
          env.PS_BUDGET_NAMESPACE || runtime?.config.namespace || 'default',
        onStoreError: section.onStoreError,
        budgets: section.budgets,
      },
      store: base.store,
      reservationTtlMs: base.reservationTtlMs,
    };
    exportLimits(section.budgets);
  }
  central = { from: section, runtime: next };
  return next;
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
  const section = centralBudgets();
  if (section) return centralRuntime(section, env);
  return runtime ?? null;
}
