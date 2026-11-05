"""AI bill teardown (``ps-teardown``, #12, #13, #14).

Internal API, in pipeline order::

    loaded = load_records(paths)                  # normalise inputs
    priced = price_records(loaded.records)        # baseline cost per record
    report = build_report(priced, loaded.sources, ReportOptions(...))
    text = render_report(report, "markdown")      # or "html", "json"

``ReportOptions(policy=...)`` adds policy replay and the sensitivity mix
(#13) on the same ``PricedRecord`` list, and ``eval_results=`` (a ``ps-eval``
JSON report) quality parity and savings at parity (#14); see ``policy.py``.
A section whose input is missing stays in ``report["pending"]``. The
normalised record schema is
:class:`TeardownRecord`; see also ``docs/teardown/cli.md``. Prices come
through the :class:`PriceSource` seam (``catalog.py``), so the versioned
catalog (#7 / #8) replaces :class:`SdkPriceSource` without touching the rest.

Everything runs locally: no network calls, no prompt is executed, and no
prompt text, response text or request hash reaches a report or an export.
"""

from prompt_shields.teardown.catalog import PriceSource, SdkPriceSource
from prompt_shields.teardown.engine import (
    PENDING,
    PricedRecord,
    ReportOptions,
    build_report,
    price_records,
)
from prompt_shields.teardown.records import (
    FORMATS,
    LoadResult,
    SourceStats,
    TeardownRecord,
    load_records,
    prompt_features,
)
from prompt_shields.teardown.render import render_report
from prompt_shields.teardown.tuples import learning_tuples, write_learning_tuples

__all__ = [
    "FORMATS",
    "PENDING",
    "LoadResult",
    "PriceSource",
    "PricedRecord",
    "ReportOptions",
    "SdkPriceSource",
    "SourceStats",
    "TeardownRecord",
    "build_report",
    "learning_tuples",
    "load_records",
    "price_records",
    "prompt_features",
    "render_report",
    "write_learning_tuples",
]
