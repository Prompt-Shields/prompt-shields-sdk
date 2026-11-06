"""Spend anomaly detector (#30): pure functions on synthetic spend series.

No clock and no randomness without a fixed seed, so every case is
deterministic. The job, webhooks and Partner API are covered against
PostgreSQL in test_spend_anomalies_db.py.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from collector import spend_anomalies as sa
from collector.spend_anomalies import DEFAULTS, config_for, detect, ewma_baseline
from db.models import SpendAlert


def _noisy(n: int, level: float, spread: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [round(level + rng.uniform(-spread, spread), 2) for _ in range(n)]


class TestEwma:
    def test_flat_series_has_its_level_and_no_spread(self):
        base = ewma_baseline([5.0] * 10, 0.3)
        assert (base.mean, base.sigma, base.history_days) == (5.0, 0.0, 10)

    def test_known_values(self):
        # m: 10 -> 13 -> 12.1; v: 0 -> 0.7*(0 + 0.3*10²) = 21 -> 0.7*(21 + 0.3*3²) = 16.59
        base = ewma_baseline([10.0, 20.0, 10.0], 0.3)
        assert base.mean == pytest.approx(12.1)
        assert base.sigma == pytest.approx(math.sqrt(16.59))

    def test_empty(self):
        assert ewma_baseline([], 0.3).history_days == 0


class TestDetect:
    def test_spike_on_a_flat_series_is_critical(self):
        found = detect([10.0] * 14, 25.0)
        assert found is not None
        # σ floors at 10% of the baseline: (25 - 10) / 1 = 15 >= 2 * 3.
        assert (found.baseline, found.sigma, found.z_score) == (10.0, 1.0, 15.0)
        assert found.severity == "critical" and found.history_days == 14

    def test_moderate_spike_is_a_warning(self):
        found = detect([10.0] * 14, 14.0)
        assert found is not None and found.severity == "warning"
        assert found.z_score == pytest.approx(4.0)

    def test_small_rise_and_drops_never_alert(self):
        assert detect([10.0] * 14, 12.5) is None  # z = 2.5 < 3
        assert detect([10.0] * 14, 0.5) is None
        assert detect([10.0] * 14, 10.0) is None

    def test_needs_min_history_after_the_first_spend(self):
        # Six days of spend after a run of zeros: a new use case, too young.
        history = [0.0] * 8 + [10.0] * 6
        assert detect(history, 100.0) is None
        assert detect(history + [10.0], 100.0) is not None
        assert sa.trim_leading_zeros([0.0, 0.0, 3.0, 0.0]) == [3.0, 0.0]

    def test_gaps_after_the_first_spend_count_as_zero(self):
        found = detect([10.0, 0.0] * 7, 30.0)
        assert found is not None and found.history_days == 14

    def test_min_delta_usd_ignores_tiny_spend(self):
        # Cents a day: a 10x jump is still well under a dollar.
        assert detect([0.02] * 14, 0.2) is None
        assert detect([0.02] * 14, 0.2, replace(DEFAULTS, min_delta_usd=0.0)) is not None

    def test_noisy_series_alerts_only_on_the_injected_spike(self):
        series = _noisy(60, level=100.0, spread=15.0, seed=30)
        series[45] = 260.0
        alerts = [
            day
            for day in range(28, len(series))
            if detect(series[day - 28 : day], series[day]) is not None
        ]
        assert alerts == [45]

    def test_threshold_and_alpha_change_the_outcome(self):
        history = _noisy(28, level=50.0, spread=10.0, seed=7)
        assert detect(history, 80.0) is not None
        assert detect(history, 80.0, replace(DEFAULTS, sigma=10.0)) is None


class TestConfig:
    def test_defaults_without_settings(self):
        assert config_for(None) == DEFAULTS
        assert config_for({}) == DEFAULTS
        assert config_for({"spend_anomaly": "nope"}) == DEFAULTS

    def test_overrides_and_invalid_fields_fall_back(self):
        cfg = config_for(
            {"spend_anomaly": {"sigma": 4, "group_by": "asset", "alpha": 7, "enabled": False}}
        )
        assert cfg.sigma == 4.0 and isinstance(cfg.sigma, float)
        assert cfg.group_by == "asset" and cfg.enabled is False
        assert cfg.alpha == DEFAULTS.alpha  # out of range: default kept

    def test_min_history_never_exceeds_the_window(self):
        cfg = config_for({"spend_anomaly": {"baseline_days": 5}})
        assert cfg.min_history_days <= cfg.baseline_days

    def test_settings_errors(self):
        assert sa.settings_errors(None) == []
        assert sa.settings_errors({"sigma": 2.5, "group_by": "use_case"}) == []
        errors = sa.settings_errors(
            {"sigma": "high", "baseline_days": 2.5, "colour": "red", "enabled": 1}
        )
        assert errors == [
            "settings.spend_anomaly.colour is not a known field",
            "settings.spend_anomaly.baseline_days must be an integer",
            "settings.spend_anomaly.enabled must be true or false",
            "settings.spend_anomaly.sigma must be a number",
        ]
        assert sa.settings_errors({"baseline_days": 5, "min_history_days": 10}) == [
            "settings.spend_anomaly.min_history_days must not exceed baseline_days"
        ]
        assert sa.settings_errors([1]) == ["settings.spend_anomaly must be an object"]


def test_days_to_check_are_complete_utc_days():
    now = datetime(2026, 11, 6, 0, 30, tzinfo=UTC)
    assert sa.days_to_check(now) == [date(2026, 11, 5)]
    assert sa.days_to_check(now, 3) == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    assert sa.days_to_check(now, day=date(2026, 1, 1)) == [date(2026, 1, 1)]
    with pytest.raises(ValueError):
        sa.days_to_check(now, 0)


def test_openapi_schemas_match_the_serializer_and_config():
    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text()
    )
    schemas = spec["components"]["schemas"]
    row = SpendAlert(
        id=uuid.uuid4(), group_by="use_case", group_key="support", day=date(2026, 3, 15),
        observed_cost=40, baseline_cost=10.0, baseline_sigma=1.0, z_score=30.0,
        threshold_sigma=3.0, history_days=28, severity="critical", status="open",
        created_at=datetime(2026, 3, 16, tzinfo=UTC),
    )
    out = sa.serialize_alert(row)
    assert set(schemas["SpendAlert"]["properties"]) == set(out)
    assert set(schemas["SpendAlert"]["required"]) == set(out)
    settings_schema = schemas["SpendAnomalySettings"]["properties"]
    assert set(settings_schema) == {f.name for f in fields(sa.SpendAnomalyConfig)}
    for name, (_, lo, hi) in sa._BOUNDS.items():
        prop = settings_schema[name]
        assert prop["minimum"] == lo and prop.get("maximum", 1_000_000.0) == hi, name
        assert prop["default"] == getattr(DEFAULTS, name), name
