"""Frozen savings baseline and counterfactual cost (PRD 0001 §7.10, #24).

A baseline records, at onboarding, which model each use case would have used
without Prompt Shields routing, and the prices of those models at that time.
``counterfactual_cost`` is a routed call's actual token usage priced on its
baseline model; savings are ``counterfactual_cost - cost``.

A ``baseline.v1`` document (``schemas/baseline/``) is self-contained:

* ``baseline_id``: the id telemetry events carry (at most 128 characters).
* ``catalog_version``: the catalog version the prices were copied from.
* ``default_model`` and ``use_cases``: ``"<provider>/<model>"`` catalog keys,
  per use case and for every other call.
* ``models``: the per-token USD rates of each baseline model, copied from
  the catalog when the baseline was frozen.

Because the rates are copied into the document, a later catalog release does
not move the counterfactual cost of past or future calls under the same
``baseline_id``. A new baseline needs a new ``baseline_id``.

The gateway implements the same rules (``gateway/src/middlewares/router/
baseline.ts``); both run the shared vectors in ``schemas/baseline/vectors/``.

The counterfactual cost is::

    uncached = max(tokens_in - tokens_cached - tokens_cache_write, 0)
    uncached * input + tokens_cached * cache_read
        + tokens_cache_write * cache_write + tokens_out * output

It is ``None`` (unpriced) when ``tokens_in`` or ``tokens_out`` is missing, or
for a service tier other than the standard ones (batch and flex calls are
discounted, and pricing them at standard rates would overstate savings).
Long-context bands and 1-hour cache-write rates are not applied: the
baseline is priced at its standard rates.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prompt_shields import catalog as _catalog
from prompt_shields.pricing import (
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    STANDARD_SERVICE_TIERS,
)

SCHEMA_VERSION = "baseline.v1"
MAX_BASELINE_ID_LENGTH = 128

#: Per-token USD rates each baseline model carries.
RATE_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
)


class BaselineError(ValueError):
    """A baseline document is invalid, or names a model it cannot price."""


def _rate(value: Any, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise BaselineError(f"{where} must be a non-negative number")
    return float(value)


def _count(event: Mapping[str, Any], name: str) -> int | None:
    value = event.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


@dataclass(frozen=True)
class Baseline:
    """A frozen ``baseline.v1`` document. Build one with ``freeze_baseline``
    at onboarding, store ``to_dict()``, and load it with ``load_baseline``."""

    baseline_id: str
    catalog_version: str
    frozen_at: str
    default_model: str
    models: Mapping[str, Mapping[str, float]]
    use_cases: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Baseline:
        """Validate a ``baseline.v1`` document. Raises ``BaselineError``."""
        if not isinstance(doc, Mapping):
            raise BaselineError("baseline must be a JSON object")
        if doc.get("schema_version") != SCHEMA_VERSION:
            raise BaselineError(f"baseline must have schema_version {SCHEMA_VERSION!r}")
        baseline_id = doc.get("baseline_id")
        if (
            not isinstance(baseline_id, str)
            or not baseline_id.strip()
            or len(baseline_id) > MAX_BASELINE_ID_LENGTH
            or "\x00" in baseline_id
        ):
            raise BaselineError(
                f"baseline_id must be a non-empty string of at most "
                f"{MAX_BASELINE_ID_LENGTH} characters"
            )
        for key in ("catalog_version", "frozen_at"):
            if not isinstance(doc.get(key), str) or not doc[key]:
                raise BaselineError(f"baseline is missing {key!r}")
        if doc.get("currency", "USD") != "USD":
            raise BaselineError("baseline currency must be 'USD'")
        raw_models = doc.get("models")
        if not isinstance(raw_models, Mapping) or not raw_models:
            raise BaselineError("baseline must list its models")
        models: dict[str, dict[str, float]] = {}
        for key, entry in raw_models.items():
            if not isinstance(entry, Mapping):
                raise BaselineError(f"models[{key!r}] must be an object")
            models[key] = {
                name: _rate(entry.get(name), f"models[{key!r}].{name}")
                for name in RATE_FIELDS
            }
        default_model = doc.get("default_model")
        if not isinstance(default_model, str) or default_model not in models:
            raise BaselineError("default_model must be one of the baseline's models")
        use_cases = doc.get("use_cases")
        if use_cases is None:
            use_cases = {}
        if not isinstance(use_cases, Mapping):
            raise BaselineError("use_cases must be an object")
        for use_case, model in use_cases.items():
            if not isinstance(model, str) or model not in models:
                raise BaselineError(
                    f"use_cases[{use_case!r}] names {model!r}, which is not in models"
                )
        return cls(
            baseline_id=baseline_id,
            catalog_version=doc["catalog_version"],
            frozen_at=doc["frozen_at"],
            default_model=default_model,
            models=models,
            use_cases=dict(use_cases),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "baseline_id": self.baseline_id,
            "catalog_version": self.catalog_version,
            "frozen_at": self.frozen_at,
            "currency": "USD",
            "default_model": self.default_model,
            "use_cases": dict(self.use_cases),
            "models": {k: dict(v) for k, v in self.models.items()},
        }

    def model_for(self, use_case: str | None) -> str:
        """The baseline model of ``use_case``, else the default model."""
        if isinstance(use_case, str) and use_case in self.use_cases:
            return self.use_cases[use_case]
        return self.default_model

    def counterfactual_cost(self, event: Mapping[str, Any]) -> float | None:
        """What the event's tokens would have cost on its baseline model, in
        USD, or ``None`` when that cannot be priced (see the module doc).
        ``event`` uses the telemetry field names (``tokens_in``,
        ``tokens_out``, ``tokens_cached``, ``tokens_cache_write``,
        ``service_tier``, ``use_case_name``)."""
        tier = event.get("service_tier")
        if tier is not None and tier not in STANDARD_SERVICE_TIERS:
            return None
        tokens_in = _count(event, "tokens_in")
        tokens_out = _count(event, "tokens_out")
        if tokens_in is None or tokens_out is None:
            return None
        cached = _count(event, "tokens_cached") or 0
        write = _count(event, "tokens_cache_write") or 0
        rates = self.models[self.model_for(event.get("use_case_name"))]
        uncached = max(tokens_in - cached - write, 0)
        # Same order of operations as the gateway, so both give the same float.
        return (
            uncached * rates["input_cost_per_token"]
            + cached * rates["cache_read_input_token_cost"]
            + write * rates["cache_creation_input_token_cost"]
            + tokens_out * rates["output_cost_per_token"]
        )

    def apply(self, event: dict) -> None:
        """Set ``baseline_id`` and, when it can be priced,
        ``counterfactual_cost`` on a routed telemetry event."""
        event["baseline_id"] = self.baseline_id
        cost = self.counterfactual_cost(event)
        if cost is not None:
            event["counterfactual_cost"] = cost


def _frozen_rates(key: str, catalog: dict) -> dict[str, float]:
    provider, _, model = key.partition("/")
    entry = _catalog.get_model(provider, model, catalog) if model else None
    if entry is None:
        raise BaselineError(
            f"{key!r} is not in the catalog (use a '<provider>/<model>' key)"
        )
    if entry.get("currency", "USD") != "USD" or "input_cost_per_token" not in entry:
        raise BaselineError(f"{key!r} has no USD token price in the catalog")
    rate_in = entry["input_cost_per_token"]
    # Cache rates the catalog leaves out: the vendor multipliers pricing.py uses.
    return {
        "input_cost_per_token": rate_in,
        "output_cost_per_token": entry["output_cost_per_token"],
        "cache_read_input_token_cost": entry.get(
            "cache_read_input_token_cost",
            rate_in * CACHE_READ_MULTIPLIER.get(provider, 1.0),
        ),
        "cache_creation_input_token_cost": entry.get(
            "cache_creation_input_token_cost",
            rate_in * CACHE_WRITE_MULTIPLIER.get(provider, 1.0),
        ),
    }


def freeze_baseline(
    baseline_id: str,
    default_model: str,
    use_cases: Mapping[str, str] | None = None,
    *,
    catalog: dict | None = None,
    frozen_at: str | None = None,
) -> Baseline:
    """Freeze a baseline at onboarding: the customer's model mix plus the
    catalog version, with each model's current rates copied in.

    Models are ``"<provider>/<model>"`` catalog keys (``"openai/gpt-4o"``).
    ``use_cases`` maps a use case name (``X-PS-Use-Case`` / ``use_case``)
    to its model; other calls use ``default_model``.
    """
    catalog = catalog or _catalog.load_catalog()
    use_cases = dict(use_cases or {})
    keys = [default_model, *use_cases.values()]
    doc = {
        "schema_version": SCHEMA_VERSION,
        "baseline_id": baseline_id,
        "catalog_version": _catalog.catalog_version(catalog),
        "frozen_at": frozen_at or datetime.now(UTC).date().isoformat(),
        "currency": "USD",
        "default_model": default_model,
        "use_cases": use_cases,
        "models": {key: _frozen_rates(key, catalog) for key in dict.fromkeys(keys)},
    }
    return Baseline.from_dict(doc)


def load_baseline(baseline: Baseline | Mapping[str, Any] | str | os.PathLike) -> Baseline:
    """A ``Baseline`` from an instance, a mapping or a ``.json`` file."""
    if isinstance(baseline, Baseline):
        return baseline
    if isinstance(baseline, Mapping):
        return Baseline.from_dict(baseline)
    try:
        doc = json.loads(Path(baseline).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BaselineError(f"cannot read baseline {baseline}: {exc}") from exc
    return Baseline.from_dict(doc)
