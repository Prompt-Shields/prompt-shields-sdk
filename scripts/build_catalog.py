"""Build packages/sdk/prompt_shields/catalog.json.

Inputs:
  - the LiteLLM price map (model_prices_and_context_window.json), for prices
    and capabilities — pass a local copy with --litellm;
  - scripts/catalog/curated.json, for model selection, provider sovereignty
    metadata and legacy entries.

Usage:
  curl -sSfL -o /tmp/litellm.json \
    https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
  python3 scripts/build_catalog.py --litellm /tmp/litellm.json [--version 2026-10-10]

Field names under "pricing" and "capabilities" are LiteLLM's, unchanged, so
the two maps stay diffable.
"""

import argparse
import datetime
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CURATED = ROOT / "scripts" / "catalog" / "curated.json"
OUT = ROOT / "packages" / "sdk" / "prompt_shields" / "catalog.json"
LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

PRICING_FIELDS = (
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
    "cache_creation_input_token_cost",
    "cache_creation_input_token_cost_above_1hr",
    "output_cost_per_reasoning_token",
    "input_cost_per_token_above_200k_tokens",
    "output_cost_per_token_above_200k_tokens",
    "cache_read_input_token_cost_above_200k_tokens",
    "input_cost_per_token_batches",
    "output_cost_per_token_batches",
    "input_cost_per_token_flex",
    "output_cost_per_token_flex",
    "input_cost_per_token_priority",
    "output_cost_per_token_priority",
)
CAPABILITY_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "supports_function_calling",
    "supports_response_schema",
    "supports_vision",
    "supports_reasoning",
    "supports_prompt_caching",
)
_DATED = re.compile(r"-\d{8}$")


def _selection(spec):
    if isinstance(spec, list):
        return "", spec
    return spec.get("litellm_prefix", ""), spec["models"]


def build(litellm: dict, curated: dict, version: str, source_sha: str) -> dict:
    providers = curated["providers"]
    models: dict[str, dict] = {}

    for vendor, spec in curated["models"].items():
        prefix, names = _selection(spec)
        for name in names:
            entry = litellm.get(prefix + name)
            if entry is None:
                raise SystemExit(f"{prefix + name}: not in the LiteLLM map")
            if entry.get("mode") != "chat":
                raise SystemExit(f"{prefix + name}: mode is {entry.get('mode')!r}, not chat")
            # Dated snapshots of this model that LiteLLM lists for the same provider.
            aliases = sorted(
                k[len(prefix):] for k, v in litellm.items()
                if isinstance(v, dict)
                and k.startswith(prefix + name + "-")
                and _DATED.fullmatch(k[len(prefix + name):])
                and v.get("litellm_provider") == entry.get("litellm_provider")
            )
            models[f"{vendor}/{name}"] = {
                "vendor": vendor,
                "model": name,
                "aliases": aliases,
                "pricing": {f: entry[f] for f in PRICING_FIELDS if entry.get(f) is not None},
                "capabilities": {f: entry[f] for f in CAPABILITY_FIELDS if entry.get(f) is not None},
                "deprecation_date": entry.get("deprecation_date"),
                "source": "litellm",
            }

    for model_id, rates in curated["legacy"].items():
        if model_id.startswith("_"):
            continue
        in_rate, out_rate = rates
        vendor, name = model_id.split("/", 1)
        if model_id in models:
            raise SystemExit(f"{model_id}: listed as legacy but present in LiteLLM")
        models[model_id] = {
            "vendor": vendor,
            "model": name,
            "aliases": [],
            "pricing": {"input_cost_per_token": in_rate, "output_cost_per_token": out_rate},
            "capabilities": {},
            "deprecation_date": None,
            "source": "legacy-sdk-0.2",
        }

    for model_id, m in models.items():
        if m["vendor"] not in providers:
            raise SystemExit(f"{model_id}: vendor {m['vendor']!r} has no provider entry")

    return {
        "schema_version": 1,
        "catalog_version": version,
        "sources": {
            "litellm": {"url": LITELLM_URL, "sha256_prefix": source_sha},
            "curated": "scripts/catalog/curated.json",
        },
        "providers": {
            pid: {k: v for k, v in p.items()} for pid, p in sorted(providers.items())
        },
        "models": dict(sorted(models.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--litellm", required=True, type=Path)
    ap.add_argument("--version", default=datetime.date.today().isoformat())
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    raw = args.litellm.read_bytes()
    catalog = build(
        json.loads(raw),
        json.loads(CURATED.read_text(encoding="utf-8")),
        args.version,
        hashlib.sha256(raw).hexdigest()[:16],
    )
    args.out.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out}: {len(catalog['models'])} models, "
          f"{len(catalog['providers'])} providers, version {args.version}")


if __name__ == "__main__":
    main()
