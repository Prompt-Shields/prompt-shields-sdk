#!/usr/bin/env python3
"""Check that the shipped dashboards and alert rules use real metric names.

Collects the metric names the code registers (gateway psMetrics registry,
collector prometheus_client), expands them to the series Prometheus scrapes
(`_total`, `_bucket`/`_sum`/`_count`), then fails when a `ps_*` name in
deploy/grafana, deploy/prometheus or the Helm chart's copies is not one of
them. Also fails when the chart's copies differ from deploy/.

Run from the repository root: python3 scripts/check-observability-metrics.py
"""

from __future__ import annotations

import filecmp
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GATEWAY = ROOT / "gateway" / "src" / "middlewares" / "psMetrics"
COLLECTOR = ROOT / "packages" / "collector" / "collector" / "metrics.py"
CHECKED = [ROOT / "deploy" / "grafana", ROOT / "deploy" / "prometheus"]
CHART_FILES = ROOT / "charts" / "prompt-shields" / "files"
COPIES = {
    ROOT / "deploy" / "prometheus" / "alerts.yaml": CHART_FILES
    / "prometheus"
    / "alerts.yaml",
}
for dash in (ROOT / "deploy" / "grafana" / "dashboards").glob("*.json"):
    COPIES[dash] = CHART_FILES / "dashboards" / dash.name

GATEWAY_RE = re.compile(r"new (Counter|Histogram)\(\s*'(ps_[a-z0-9_]+)'")
COLLECTOR_RE = re.compile(r"\b(Counter|Histogram)\(\s*\"(ps_[a-z0-9_]+)\"")
USED_RE = re.compile(r"\b(ps_(?:gateway|collector)_[a-z0-9_]+)")


def series(kind: str, name: str, prometheus_client: bool) -> set[str]:
    if kind == "Histogram":
        return {f"{name}_bucket", f"{name}_sum", f"{name}_count"}
    if prometheus_client:
        base = name.removesuffix("_total")
        return {f"{base}_total"}
    return {name}


def known() -> set[str]:
    names: set[str] = set()
    for ts in GATEWAY.glob("*.ts"):
        if ts.name.endswith(".test.ts"):
            continue
        for kind, name in GATEWAY_RE.findall(ts.read_text()):
            names |= series(kind, name, prometheus_client=False)
    for kind, name in COLLECTOR_RE.findall(COLLECTOR.read_text()):
        names |= series(kind, name, prometheus_client=True)
    return names


def main() -> int:
    names = known()
    if not names:
        print("no metric definitions found; did the source move?")
        return 1
    errors = []
    files = [
        p
        for d in CHECKED
        for p in d.rglob("*")
        if p.suffix in {".json", ".yaml", ".yml"}
    ]
    for path in files:
        for used in sorted(set(USED_RE.findall(path.read_text()))):
            if used not in names:
                errors.append(f"{path.relative_to(ROOT)}: unknown metric {used}")
    for src, copy in COPIES.items():
        if not copy.exists() or not filecmp.cmp(src, copy, shallow=False):
            errors.append(
                f"{copy.relative_to(ROOT)} differs from {src.relative_to(ROOT)}; copy it over"
            )
    for e in errors:
        print(e)
    print(f"checked {len(files)} files against {len(names)} metric series")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
