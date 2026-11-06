"""FOCUS cost export (#31) without a database: column conformance against
the FOCUS v1.4 Cost and Usage column list, the row mapping, and CSV / JSON
Lines serialization (including CSV formula injection)."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from collector import focus_export as fx
from collector.partner_scopes import LEGACY_REGISTRY_READ, SCOPES, expand_scopes

SPEC = json.loads(
    (
        Path(__file__).parent / "fixtures" / "focus" / "focus_1_4_cost_and_usage_columns.json"
    ).read_text()
)
SPEC_COLUMNS = {c["column_id"]: c for c in SPEC["columns"]}
TENANT = uuid.uuid4()
ASSET = uuid.uuid4()
WINDOW = (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _agg(**over):
    row = {
        "bucket": datetime(2026, 10, 5),  # naive UTC, as date_trunc returns it
        "asset_id": ASSET,
        "vendor": "openai",
        "use_case_name": "support",
        "business_unit": "ops",
        "environment": "prod",
        "region": "swedencentral",
        "model": "gpt-4o-2024-08-06",
        "provider": "azure-openai-swe",
        "requests": 3,
        "cost": Decimal("0.012000"),
        "tokens_in": 1000,
        "tokens_out": 200,
        "tokens_reported": 6,
    }
    row.update(over)
    return row


def _row(agg=None, **kw):
    args = {
        "tenant_id": TENANT,
        "tenant_name": "Acme",
        "window_start": WINDOW[0],
        "window_end": WINDOW[1],
        "granularity": "day",
        "cost_mode": "estimated",
    }
    args.update(kw)
    return fx.focus_row(agg or _agg(), **args)


SPARSE = {
    "use_case_name": None,
    "business_unit": None,
    "environment": None,
    "region": None,
    "tokens_in": None,
    "tokens_out": None,
    "tokens_reported": 0,
    "requests": 1,
}


# ──────────────────────────────────────────────────────────────────────
# Column conformance
# ──────────────────────────────────────────────────────────────────────


def test_spec_fixture_is_focus_1_4_cost_and_usage():
    assert SPEC["focus_version"] == fx.FOCUS_VERSION == "1.4"
    assert len(SPEC_COLUMNS) == 65
    # v1.4 removed the deprecated provider columns.
    assert "ProviderName" not in SPEC_COLUMNS
    assert "PublisherName" not in SPEC_COLUMNS


@pytest.mark.parametrize("level", ["Mandatory", "Recommended"])
def test_every_mandatory_and_recommended_column_is_exported(level):
    required = {c for c, d in SPEC_COLUMNS.items() if d["feature_level"] == level}
    assert required, level
    assert sorted(required - set(fx.FOCUS_COLUMNS)) == []


def test_every_exported_focus_column_is_in_the_spec():
    assert sorted(set(fx.FOCUS_COLUMNS) - set(SPEC_COLUMNS)) == []


def test_columns_are_unique_and_custom_columns_use_the_x_prefix():
    assert len(fx.COLUMNS) == len(set(fx.COLUMNS))
    for col in fx.CUSTOM_COLUMNS:
        # CustomColumnHandling: `x_` prefix, Pascal case alphanumerics after it.
        assert re.fullmatch(r"x_[A-Z][A-Za-z0-9]*", col), col
        assert len(col) <= 50
        assert col[2:] not in SPEC_COLUMNS, col


@pytest.mark.parametrize("agg", [_agg(), _agg(**SPARSE)], ids=["full", "sparse"])
@pytest.mark.parametrize("mode", ["estimated", "merge_safe"])
def test_rows_respect_the_spec_null_and_type_constraints(agg, mode):
    row = _row(agg, cost_mode=mode)
    assert list(row) == list(fx.COLUMNS)
    for col in fx.FOCUS_COLUMNS:
        spec = SPEC_COLUMNS[col]
        value = row[col]
        if not spec["allows_nulls"]:
            assert value is not None, col
        if value is None:
            continue
        kind = spec["data_type"]
        if kind == "Decimal":
            assert isinstance(value, Decimal), col
        elif kind == "Date/Time":
            assert DATETIME.match(value), (col, value)
        elif kind == "JSON":
            assert isinstance(value, dict) and value, col
            assert all(not isinstance(v, (dict, list)) for v in value.values())
        else:
            assert isinstance(value, str) and value != "", col


def test_allowed_values_and_cost_relationships():
    row = _row()
    assert row["ChargeCategory"] == "Usage"
    assert row["ChargeFrequency"] == "Usage-Based"
    assert row["ChargeClass"] is None
    assert row["ServiceCategory"] == "AI and Machine Learning"
    assert row["ServiceSubcategory"] == "Generative AI"
    assert row["BillingCurrency"] == "USD"
    # No unit prices exported, so FOCUS requires List/Contracted = Billed;
    # an uncovered Usage charge has Effective = Billed.
    assert row["BilledCost"] == row["EffectiveCost"] == row["ListCost"] == row["ContractedCost"]
    assert row["BilledCost"] == Decimal("0.012000")
    # HostProviderName matches ServiceProviderName unless a host is exposed.
    assert row["HostProviderName"] == row["ServiceProviderName"] == "azure-openai-swe"
    assert row["InvoiceIssuerName"] == "azure-openai-swe"
    assert row["ServiceName"] == "openai"
    assert row["ResourceId"] == str(ASSET)
    assert row["ResourceName"] != row["ResourceId"]
    assert row["SkuId"] == row["x_ModelName"] == "gpt-4o-2024-08-06"
    assert row["PricingQuantity"] == row["ConsumedQuantity"] == Decimal(1200)
    assert row["PricingUnit"] == row["ConsumedUnit"] == "Tokens"
    assert row["Tags"] == {
        "ps:business_unit": "ops",
        "ps:use_case": "support",
        "ps:environment": "prod",
    }
    assert row["ChargeDescription"] == "gpt-4o-2024-08-06 usage (3 requests)"


def test_merge_safe_zeroes_cost_columns_and_keeps_the_estimate():
    row = _row(cost_mode="merge_safe")
    for col in ("BilledCost", "EffectiveCost", "ListCost", "ContractedCost"):
        assert row[col] == 0
    assert row["x_EstimatedCost"] == Decimal("0.012000")


def test_sparse_row_nulls_quantities_and_units_together():
    row = _row(_agg(**SPARSE))
    for col in ("PricingQuantity", "PricingUnit", "ConsumedQuantity", "ConsumedUnit"):
        assert row[col] is None
    assert row["Tags"] is None
    assert row["ResourceName"] is None
    assert row["RegionId"] is None
    assert row["x_InputTokens"] is None
    assert row["ChargeDescription"].endswith("(1 request)")


def test_charge_and_billing_periods():
    row = _row(
        granularity="hour",
        agg=_agg(bucket=datetime(2026, 12, 31, 23)),
        window_start=datetime(2026, 12, 1, tzinfo=UTC),
        window_end=datetime(2027, 1, 2, tzinfo=UTC),
    )
    assert row["ChargePeriodStart"] == "2026-12-31T23:00:00Z"
    assert row["ChargePeriodEnd"] == "2027-01-01T00:00:00Z"
    assert row["BillingPeriodStart"] == "2026-12-01T00:00:00Z"
    assert row["BillingPeriodEnd"] == "2027-01-01T00:00:00Z"


def test_bucket_cut_by_the_window_only_covers_the_window():
    row = _row(
        window_start=datetime(2026, 10, 5, 6, tzinfo=UTC),
        window_end=datetime(2026, 10, 5, 18, tzinfo=UTC),
    )
    assert row["ChargePeriodStart"] == "2026-10-05T06:00:00Z"
    assert row["ChargePeriodEnd"] == "2026-10-05T18:00:00Z"


# ──────────────────────────────────────────────────────────────────────
# Serialization
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
        ("+1+1", "'+1+1"),
        ("-2+3", "'-2+3"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tcmd", "'\tcmd"),
        ("\rcmd", "'\rcmd"),
        ("support", "support"),
        ("", ""),
        (Decimal("-1.5"), Decimal("-1.5")),
        (3, 3),
    ],
)
def test_csv_safe(value, expected):
    assert fx.csv_safe(value) == expected


def test_csv_neutralises_formulas_from_telemetry():
    evil = _agg(use_case_name="=cmd|' /C calc'!A0", model="@SUM(1+1)", provider="+x")
    text = fx.serialize_csv([_row(evil)], header=True)
    header, line = list(csv.reader(io.StringIO(text)))
    assert header == list(fx.COLUMNS)
    cells = dict(zip(header, line, strict=True))
    assert cells["ResourceName"] == "'=cmd|' /C calc'!A0"
    assert cells["SkuId"] == "'@SUM(1+1)"
    assert cells["ServiceProviderName"] == "'+x"
    assert json.loads(cells["Tags"])["ps:use_case"] == "=cmd|' /C calc'!A0"
    for value in line:
        assert not value.startswith(("=", "+", "-", "@", "\t", "\r")), value
    # Numbers are plain decimals, nulls empty cells.
    assert cells["BilledCost"] == "0.012000"
    assert cells["ChargeClass"] == ""


def test_jsonl_has_one_object_per_line_in_column_order():
    text = fx.serialize_jsonl([_row(), _row(_agg(**SPARSE))])
    lines = text.splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert list(first) == list(fx.COLUMNS)
    assert first["BilledCost"] == 0.012
    assert first["Tags"]["ps:business_unit"] == "ops"
    assert json.loads(lines[1])["PricingQuantity"] is None


def test_usage_export_scope_is_never_granted_by_registry_read():
    assert "usage:export" in SCOPES
    assert "usage:export" not in LEGACY_REGISTRY_READ
    assert "usage:export" not in expand_scopes(["registry:read"])
