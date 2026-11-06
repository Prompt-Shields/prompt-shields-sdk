"""Named OpenAI-compatible providers, mostly EU sovereign ones (#20).

One adapter serves them all: the OpenAI SDK with the provider's ``base_url``
and a bearer API key. This module only supplies the per-provider settings
(base URL, region, the catalog's sovereignty facts), taken from the model
catalog (``catalog.json``, ``providers.<id>.api_base``), so the SDK and the
gateway's provider configs agree (a gateway test checks the same source).

``ShieldsRouter`` uses it when a provider entry names one of these providers
and gives no ``base_url``::

    ShieldsRouter(providers={"scaleway": {"api_key": "..."}}, ...)
    ShieldsRouter(providers={"eu-llama": {"api_key": "...",
                                          "catalog_provider": "ionos"}}, ...)

Sovereignty facts here describe the provider and are never applied to a
route policy on their own: the policy author states them per tier.
``catalog_tier`` copies the verified ones into a tier, leaving every
unverified (``None``) fact out so that it fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from prompt_shields import catalog as _catalog

# The region each base URL serves, where the vendor names one. Only the
# region in the catalog's base URL is known; a provider's other regions are
# not catalogued, so asking for one is refused (pass ``base_url`` instead).
_REGIONS: dict[str, str] = {
    "ionos": "de-txl",
    "stackit": "eu01",
    "scaleway": "fr-par",
}

# OpenAI itself is reached with vendor "openai" and no base_url.
_EXCLUDED = frozenset({"openai"})


class SovereignProviderError(ValueError):
    """Unknown provider, or a region with no known base URL."""


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """Settings for one OpenAI-compatible provider."""

    name: str
    display_name: str
    base_url: str
    region: str | None
    jurisdiction: str | None
    hosting_countries: tuple[str, ...] | None
    us_cloud_act_exposure: bool | None
    zdr: bool | None
    #: How the API key is sent. Every provider here takes
    #: ``Authorization: Bearer <key>``.
    auth: str = "bearer"


def _build(catalog: dict | None = None) -> dict[str, OpenAICompatibleProvider]:
    out = {}
    for pid, p in (catalog or _catalog.load_catalog())["providers"].items():
        if pid in _EXCLUDED or not p.get("openai_compatible") or not p.get("api_base"):
            continue
        hosting = p.get("hosting_countries")
        out[pid] = OpenAICompatibleProvider(
            name=pid,
            display_name=p.get("name") or pid,
            base_url=p["api_base"],
            region=_REGIONS.get(pid),
            jurisdiction=p.get("jurisdiction"),
            hosting_countries=tuple(hosting) if hosting else None,
            us_cloud_act_exposure=p.get("us_cloud_act_exposure"),
            zdr=p.get("zdr"),
        )
    return out


#: Every OpenAI-compatible provider in the bundled catalog with a base URL:
#: Mistral (EU), Berget, Infercom, Regolo, IONOS, STACKIT, Scaleway, OVHcloud.
OPENAI_COMPATIBLE_PROVIDERS: dict[str, OpenAICompatibleProvider] = _build()


def get_openai_compatible(name: str) -> OpenAICompatibleProvider | None:
    return OPENAI_COMPATIBLE_PROVIDERS.get(name)


def resolve_base_url(name: str, region: str | None = None) -> str:
    """The base URL for provider ``name`` (in ``region``, when given)."""
    p = OPENAI_COMPATIBLE_PROVIDERS.get(name)
    if p is None:
        known = ", ".join(sorted(OPENAI_COMPATIBLE_PROVIDERS))
        raise SovereignProviderError(
            f"unknown OpenAI-compatible provider {name!r} (known: {known}); "
            "pass base_url"
        )
    if region is not None and region != p.region:
        raise SovereignProviderError(
            f"provider {name!r}: no known base URL for region {region!r}"
            + (f" (catalogued: {p.region!r})" if p.region else "")
            + "; pass base_url"
        )
    return p.base_url


def provider_config(
    name: str, api_key: str, *, region: str | None = None, **options: Any
) -> dict:
    """A ``ShieldsRouter`` provider entry for a named provider.

    ``provider_config("scaleway", key)`` ->
    ``{"api_key": key, "vendor": "openai", "base_url": "https://api.scaleway.ai/v1"}``.
    Extra ``options`` (``timeout``, ``max_retries``, ...) go to the client.
    """
    return {
        "api_key": api_key,
        "vendor": "openai",
        "base_url": resolve_base_url(name, region),
        **options,
    }


def catalog_tier(
    provider: str,
    model: str,
    *,
    input_cost_per_1k: float | None = None,
    output_cost_per_1k: float | None = None,
    catalog: dict | None = None,
) -> dict:
    """A route-policy.v1 tier for ``provider/model`` with the catalog's facts.

    ``jurisdiction``, ``zdr`` and ``us_cloud_act_exposure`` are copied from
    the catalog model entry; a ``None`` (unverified) fact is left out, so the
    tier fails closed on it. Prices come from the arguments, else from the
    catalog when it lists USD prices. EUR-priced entries need explicit USD
    prices: route policies price in USD.
    """
    entry = _catalog.get_model(provider, model, catalog)
    if entry is None:
        raise SovereignProviderError(f"{provider}/{model} is not in the catalog")
    if input_cost_per_1k is None or output_cost_per_1k is None:
        if entry.get("currency", "USD") != "USD" or "input_cost_per_token" not in entry:
            raise SovereignProviderError(
                f"{provider}/{model} has no USD price in the catalog; pass "
                "input_cost_per_1k and output_cost_per_1k in USD"
            )
        if input_cost_per_1k is None:
            input_cost_per_1k = round(entry["input_cost_per_token"] * 1000, 12)
        if output_cost_per_1k is None:
            output_cost_per_1k = round(entry["output_cost_per_token"] * 1000, 12)
    tier: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "input_cost_per_1k": input_cost_per_1k,
        "output_cost_per_1k": output_cost_per_1k,
    }
    for field in ("jurisdiction", "zdr", "us_cloud_act_exposure"):
        if entry.get(field) is not None:
            tier[field] = entry[field]
    return tier
