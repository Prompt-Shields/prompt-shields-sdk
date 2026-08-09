import { ModelTier, RoutePolicy, RouteRequest } from './types';

/** Assumed completion length when the caller gives no bound. */
export const DEFAULT_OUTPUT_TOKENS = 512;

/**
 * Rough token estimate: ~4 characters per token across concatenated message
 * content. Cheap and deterministic — good enough to separate "one line" from
 * "a wall of context". A tokenizer can replace this without touching callers.
 */
export function estimateInputTokens(messages: any[]): number {
  let chars = 0;
  for (const m of messages || []) {
    const content = m?.content;
    if (typeof content === 'string') {
      chars += content.length;
    } else if (Array.isArray(content)) {
      for (const part of content) {
        if (typeof part?.text === 'string') chars += part.text.length;
      }
    }
  }
  return Math.ceil(chars / 4);
}

/** Estimated USD cost of running one tier for the given token counts. */
export function estimateTierCost(
  tier: ModelTier,
  inputTokens: number,
  outputTokens: number
): number {
  return (
    (inputTokens / 1000) * tier.inputCostPer1k +
    (outputTokens / 1000) * tier.outputCostPer1k
  );
}

function outTokens(req: RouteRequest): number {
  return req.maxOutputTokens ?? DEFAULT_OUTPUT_TOKENS;
}

/** Cheapest tier within a single group for this request. */
export function pickCheapestIn(tiers: ModelTier[], req: RouteRequest) {
  const out = outTokens(req);
  let best: ModelTier | null = null;
  let bestCost = Infinity;
  for (const tier of tiers || []) {
    const cost = estimateTierCost(tier, req.approxInputTokens, out);
    if (cost < bestCost) {
      best = tier;
      bestCost = cost;
    }
  }
  return best ? { tier: best, estCost: bestCost } : null;
}

/**
 * Cheapest tier across ALL groups whose estimated cost fits `maxCost`. When
 * nothing fits, returns the globally cheapest tier and flags `underBudget:false`
 * so the caller can record that the ceiling could not be honored.
 */
export function cheapestUnderBudget(
  policy: RoutePolicy,
  maxCost: number,
  req: RouteRequest
) {
  const out = outTokens(req);
  let underBudget: { tier: ModelTier; estCost: number } | null = null;
  let cheapest: { tier: ModelTier; estCost: number } | null = null;

  for (const group of Object.keys(policy.groups)) {
    for (const tier of policy.groups[group]) {
      const estCost = estimateTierCost(tier, req.approxInputTokens, out);
      if (!cheapest || estCost < cheapest.estCost) cheapest = { tier, estCost };
      if (
        estCost <= maxCost &&
        (!underBudget || estCost > underBudget.estCost)
      ) {
        // Prefer the *most capable* option still under budget (highest cost ≤ ceiling).
        underBudget = { tier, estCost };
      }
    }
  }

  if (underBudget) return { ...underBudget, underBudget: true };
  return cheapest ? { ...cheapest, underBudget: false } : null;
}

/**
 * Default policy. Groups and pricing mirror the SDK's built-in pricing table
 * (packages/sdk/prompt_shields/pricing.py). Override per deployment / virtual-key.
 */
export const defaultPolicy: RoutePolicy = {
  defaultGroup: 'balanced',
  qualityToGroup: {
    draft: 'cheap',
    balanced: 'balanced',
    critical: 'frontier',
  },
  groups: {
    cheap: [
      {
        model: 'gpt-4o-mini',
        group: 'cheap',
        inputCostPer1k: 0.00015,
        outputCostPer1k: 0.0006,
      },
      {
        model: 'claude-3-5-haiku-20241022',
        group: 'cheap',
        inputCostPer1k: 0.0008,
        outputCostPer1k: 0.004,
      },
    ],
    balanced: [
      {
        model: 'gpt-4o',
        group: 'balanced',
        inputCostPer1k: 0.0025,
        outputCostPer1k: 0.01,
      },
    ],
    frontier: [
      {
        model: 'claude-sonnet-4-20250514',
        group: 'frontier',
        inputCostPer1k: 0.003,
        outputCostPer1k: 0.015,
      },
      {
        model: 'gpt-4o',
        group: 'frontier',
        inputCostPer1k: 0.0025,
        outputCostPer1k: 0.01,
      },
    ],
  },
};
