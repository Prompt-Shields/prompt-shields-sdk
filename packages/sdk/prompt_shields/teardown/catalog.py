"""The pricing seam for ``ps-teardown``.

Everything the teardown needs from a price list goes through
:class:`PriceSource`: the catalog version, a model's priced id, the cost of a
record's usage at a (provider, model), why a cost is null, which other
(provider, model) pairs serve the same model, and whether a candidate's
price list covers a record (for same-model arbitrage).

:class:`SdkPriceSource` implements it on top of ``prompt_shields.pricing``
(the 2026-Q1 snapshot). When the versioned catalog (#7 / #8) lands, a new
``PriceSource`` replaces it here and nothing else in the teardown changes.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from prompt_shields import pricing
from prompt_shields.teardown.records import TeardownRecord

# Null-cost reasons (methodology M§4.5).
NULL_NO_MODEL = "no_model"
NULL_AUTO_WITHOUT_SERVED = "auto_without_served_model"
NULL_MISSING_TOKENS = "missing_tokens"
NULL_UNKNOWN_MODEL = "unknown_model"
NULL_UNPRICED_TIER = "unpriced_service_tier"
NULL_AUDIO_WITHOUT_RATE = "audio_without_rate"
NULL_OTHER = "unpriced"

#: (provider, model) pairs that are the same model sold by different hosts.
#: Same-model arbitrage only compares within one group. The 2026-Q1 table
#: lists the same token prices for every host in a group, so arbitrage on
#: the default table is zero until the catalog (#7) adds hosts and regions.
DEFAULT_EQUIVALENTS: tuple[tuple[tuple[str, str], ...], ...] = (
    (
        ("anthropic", "claude-sonnet-4-20250514"),
        ("bedrock", "anthropic.claude-sonnet-4-20250514-v1:0"),
    ),
    (
        ("anthropic", "claude-3-5-sonnet-20241022"),
        ("bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0"),
    ),
    (
        ("anthropic", "claude-3-5-haiku-20241022"),
        ("bedrock", "anthropic.claude-3-5-haiku-20241022-v1:0"),
    ),
)

#: Above this many prompt tokens a call may be in a long-context band (the
#: lowest band threshold in ``pricing.LONG_CONTEXT_PRICING``).
LONG_PROMPT_TOKENS = min(b[0] for b in pricing.LONG_CONTEXT_PRICING.values())

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_BEDROCK_PROFILE = re.compile(r"^(?:us|eu|apac|us-gov|global|jp|au|ca)\.")


class PriceSource(Protocol):
    """What the teardown needs from a price list."""

    #: Recorded in the report's methodology record (M§4.4).
    version: str

    def resolve_model(self, provider: str | None, model: str | None) -> str | None:
        """The priced model id for ``model`` at ``provider``, or None."""

    def cost(self, record: TeardownRecord, provider: str, model: str) -> float | None:
        """USD cost of ``record``'s usage at (provider, model); None if unpriced."""

    def null_reason(self, record: TeardownRecord) -> str:
        """Why ``record`` has no cost (one of the ``NULL_*`` reasons)."""

    def equivalents(self, provider: str, model: str) -> list[tuple[str, str]]:
        """Other (provider, model) pairs that serve the same model."""

    def covers(self, record: TeardownRecord, provider: str, model: str) -> bool:
        """Whether (provider, model) has a price for every dimension ``record`` uses.

        Arbitrage only moves a call to a host whose price list covers it, so
        a missing rate (web-search fee, 1-hour cache write, long-context band,
        audio) never shows up as a saving (M§4.6: when unsure, take the more
        expensive option).
        """


def _sdk_catalog_version() -> str:
    """``sdk-<version>@<sha256 of pricing.py, 12 hex>``: no catalog id exists yet."""
    from prompt_shields import __version__

    try:
        digest = hashlib.sha256(Path(pricing.__file__).read_bytes()).hexdigest()[:12]
    except OSError:
        digest = "unknown"
    return f"sdk-{__version__}@{digest}"


class SdkPriceSource:
    """``PriceSource`` over ``prompt_shields.pricing``.

    ``pricing_table`` replaces ``DEFAULT_PRICING`` (same key and tuple
    layout; long-context bands then do not apply, as in ``pricing.py``);
    ``equivalents`` replaces ``DEFAULT_EQUIVALENTS``; ``version`` overrides
    the computed catalog version (tests pin it).
    """

    def __init__(
        self,
        pricing_table: Mapping[tuple[str, str], tuple[float, ...]] | None = None,
        equivalents: Iterable[Iterable[tuple[str, str]]] | None = None,
        version: str | None = None,
    ) -> None:
        self._custom = dict(pricing_table) if pricing_table is not None else None
        self._table = self._custom if self._custom is not None else pricing.DEFAULT_PRICING
        self.version = version or (
            "custom-pricing-table" if self._custom is not None else _sdk_catalog_version()
        )
        self._groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for group in equivalents if equivalents is not None else DEFAULT_EQUIVALENTS:
            members = [tuple(m) for m in group]
            for m in members:
                self._groups[m] = [o for o in members if o != m]

    # -- model lookup --------------------------------------------------------

    def _vendor(self, provider: str, model: str) -> str:
        """The vendor whose rates price (provider, model), after fallback."""
        if (provider, model) not in self._table:
            return pricing.PRICING_VENDOR_FALLBACK.get(provider, provider)
        return provider

    def _known(self, provider: str, model: str) -> bool:
        if (self._vendor(provider, model), model) in self._table:
            return True
        if any(k[0] == provider and k[1] == model for k in pricing.UNIT_PRICING):
            return True
        return (provider, model) in pricing.IMAGE_PRICING

    def resolve_model(self, provider: str | None, model: str | None) -> str | None:
        if not provider or not model:
            return None
        bare = model.rsplit("models/", 1)[-1]
        for candidate in (model, bare, _DATE_SUFFIX.sub("", bare), _BEDROCK_PROFILE.sub("", bare)):
            if self._known(provider, candidate):
                return candidate
        return None

    # -- pricing -------------------------------------------------------------

    @staticmethod
    def _fields(record: TeardownRecord) -> dict[str, Any]:
        return {
            "tokens_in": record.tokens_in,
            "tokens_out": record.tokens_out,
            "tokens_cached": record.tokens_cached,
            "tokens_cache_write": record.tokens_cache_write,
            "tokens_cache_write_1h": record.tokens_cache_write_1h,
            "tokens_audio_in": record.tokens_audio_in,
            "tokens_audio_out": record.tokens_audio_out,
            "web_search_requests": record.web_search_requests,
            "service_tier": record.service_tier,
            "usage_units": record.usage_units,
            "usage_unit_type": record.usage_unit_type,
            "image_quality": record.image_quality,
            "image_size": record.image_size,
        }

    def _table_for(self, record: TeardownRecord, provider: str, model: str) -> dict | None:
        """The table to price with: None means the defaults, bands included.

        An aggregated row sums many requests, so its ``tokens_in`` says
        nothing about one request's size: the long-context band must not be
        chosen from it. It is priced at base rates, or at band rates when
        the export reports the band (Anthropic ``context_window``).
        """
        if self._custom is not None:
            return self._custom
        if not record.aggregated:
            return None
        table = dict(pricing.DEFAULT_PRICING)
        key = (self._vendor(provider, model), model)
        band = pricing.LONG_CONTEXT_PRICING.get(key)
        if band is not None and record.context_window not in (None, "0-200k"):
            table[key] = band[1]
        return table

    def cost(self, record: TeardownRecord, provider: str, model: str) -> float | None:
        return pricing.usage_cost(
            provider, model, self._fields(record), self._table_for(record, provider, model)
        )

    def null_reason(self, record: TeardownRecord) -> str:
        model = record.pricing_model
        if not model:
            return NULL_NO_MODEL
        if model == "auto" and not record.served_model:
            return NULL_AUTO_WITHOUT_SERVED
        priced = self.resolve_model(record.provider, model)
        if priced is None:
            return NULL_UNKNOWN_MODEL
        if record.usage_unit_type is None and (record.tokens_in is None or record.tokens_out is None):
            return NULL_MISSING_TOKENS
        vendor = self._vendor(record.provider or "", priced)
        tier = record.service_tier
        if (
            tier is not None
            and tier not in pricing.STANDARD_SERVICE_TIERS
            and tier not in pricing.SERVICE_TIER_MULTIPLIER.get(vendor, {})
        ):
            return NULL_UNPRICED_TIER
        if (record.tokens_audio_in or record.tokens_audio_out) and (
            (vendor, priced) not in pricing.AUDIO_PRICING
        ):
            return NULL_AUDIO_WITHOUT_RATE
        return NULL_OTHER

    def equivalents(self, provider: str, model: str) -> list[tuple[str, str]]:
        return list(self._groups.get((provider, model), []))

    def covers(self, record: TeardownRecord, provider: str, model: str) -> bool:
        vendor = self._vendor(provider, model)
        rates = self._table.get((vendor, model), ())
        if record.web_search_requests and vendor not in pricing.WEB_SEARCH_FEE:
            return False
        if (
            record.tokens_cache_write_1h
            and len(rates) <= 4
            and vendor not in pricing.CACHE_WRITE_1H_MULTIPLIER
        ):
            return False
        if (record.tokens_audio_in or record.tokens_audio_out) and (
            (vendor, model) not in pricing.AUDIO_PRICING
        ):
            return False
        # A long prompt (or an export row in a long-context band) moves only
        # to a candidate that prices long context itself; otherwise its base
        # rate would understate the cost.
        long_prompt = (
            record.context_window not in (None, "0-200k")
            if record.aggregated
            else (record.tokens_in or 0) > LONG_PROMPT_TOKENS
        )
        return not (long_prompt and (self._custom is not None or (vendor, model) not in pricing.LONG_CONTEXT_PRICING))
