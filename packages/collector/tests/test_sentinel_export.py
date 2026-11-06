"""Microsoft Sentinel export (#32) without a database: mapping, schema
docs, configuration checks and the Entra + Logs Ingestion client against
mocked endpoints. No network."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from collector import audit_trail
from collector import sentinel_export as se
from collector.connectors import ConnectorError
from collector.partner_scopes import LEGACY_REGISTRY_READ, SCOPES, expand_scopes
from db import audit_chain
from db.models import AIAsset, AIUsageEvent, AuditEvent
from pydantic import ValidationError

DOCS = Path(__file__).resolve().parents[3] / "docs" / "integrations" / "sentinel"
SECRET = "entra-CLIENT-SECRET-value"
TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
ENTRA_TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT_ID = "33333333-3333-3333-3333-333333333333"
DCR = "dcr-" + "a" * 32
INGEST_HOST = "ps-dcr-x1y2.westeurope-1.ingest.monitor.azure.com"


def _conn(**over) -> dict:
    return {
        "entra_tenant_id": ENTRA_TENANT,
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "ingestion_endpoint": f"https://{INGEST_HOST}",
        "dcr_immutable_id": DCR,
        **over,
    }


def _settings(**over) -> se.SentinelSettings:
    return se.SentinelSettings(sentinel_export={str(TENANT): _conn(**over)})


def _event(**over) -> AIUsageEvent:
    fields = {
        "id": uuid.uuid4(),
        "tenant_id": TENANT,
        "asset_id": uuid.uuid4(),
        "timestamp": datetime(2026, 11, 1, 9, 0, tzinfo=UTC),
        "source": "sdk",
        "tokens_in": 120,
        "tokens_out": 30,
        "cost": Decimal("0.001200"),
        "latency_ms": 420,
        "prompt_hash": "sha256-of-the-prompt",
        "session_id": "user-free-text-session",
        "api_key_fingerprint": "0123456789abcdef",
        "flow_context": {"pii_categories": ["EMAIL"]},
        "requested_model": "auto",
        "served_model": "mistral-large-2411",
        "served_provider": "mistral",
        "served_jurisdiction": "EEA",
        "policy_version": "2026-10-30.3",
        "sensitivity_level": "confidential",
        "sensitivity_source": "pii_escalated",
        "route_mode": "gateway",
        "route_group": "eu-only",
        "route_reason": "cheapest_compliant",
        "route_est_cost": Decimal("0.001000"),
        "fallback_chain": ["rate_limited"],
        "counterfactual_cost": Decimal("0.004000"),
        "baseline_id": "gpt-4o",
        "call_id": "call-abc",
    }
    fields.update(over)
    return AIUsageEvent(**fields)


def _audit_row(**over) -> AuditEvent:
    row = AuditEvent(
        id=uuid.uuid4(),
        tenant_id=TENANT,
        seq=7,
        created_at=datetime(2026, 11, 1, 9, 5, tzinfo=UTC),
        action="partner_credential.update",
        outcome="success",
        actor_type="tenant_key",
        actor_id="abcd1234",
        target_type="partner_credential",
        target_id="p-1",
        request_id="req-1",
        status_code=200,
        details={"scopes": ["audit:read"]},
        prev_hash="0" * 64,
    )
    for k, v in over.items():
        setattr(row, k, v)
    row.row_hash = audit_chain.compute_row_hash(row.prev_hash, audit_trail.chained_fields(row))
    return row


# ── mapping ─────────────────────────────────────────────────────────────


def test_call_record_carries_routing_evidence_and_no_content():
    asset = AIAsset(vendor="mistral", business_unit="Retail", environment="production")
    ev = _event()
    rec = se.call_record(ev, asset)
    assert rec["EventType"] == "AICall" and rec["Operation"] == "ai.call"
    assert rec["EventUid"] == str(ev.id)
    assert rec["TimeGenerated"] == "2026-11-01T09:00:00Z"
    # The acceptance criterion: jurisdiction, policy version, sensitivity.
    assert rec["PsServedJurisdiction"] == "EEA" and rec["PsSovereign"] is True
    assert rec["PsPolicyVersion"] == "2026-10-30.3"
    assert rec["PsSensitivityLevel"] == "confidential"
    assert rec["PsSensitivitySource"] == "pii_escalated"
    assert rec["PsFallbackChain"] == ["rate_limited"]
    assert rec["PsCostUsd"] == pytest.approx(0.0012)
    assert rec["PsBusinessUnit"] == "Retail" and rec["PsVendor"] == "mistral"
    text = json.dumps(rec)
    for leaked in ("sha256-of-the-prompt", "user-free-text-session", "0123456789abcdef", "EMAIL"):
        assert leaked not in text
    assert set(rec) <= se.COLUMN_NAMES


def test_unknown_jurisdiction_is_not_called_sovereign_or_not():
    rec = se.call_record(_event(served_jurisdiction=None, policy_version=None))
    assert "PsSovereign" not in rec and "PsPolicyVersion" not in rec
    assert se.call_record(_event(served_jurisdiction="us"))["PsSovereign"] is False
    assert se.call_record(_event(served_jurisdiction="de"))["PsSovereign"] is True


def test_audit_record_keeps_the_chain_hashes():
    row = _audit_row()
    rec = se.audit_record(row)
    assert rec["EventType"] == "Audit" and rec["Operation"] == "partner_credential.update"
    assert rec["PsAuditSeq"] == 7 and rec["PsAuditRowHash"] == row.row_hash
    assert rec["PsAuditPrevHash"] == "0" * 64
    assert rec["EventResult"] == "Success" and rec["EventSeverity"] == "Informational"
    assert rec["PsAuditDetails"] == {"scopes": ["audit:read"]}
    assert set(rec) <= se.COLUMN_NAMES
    failed = se.audit_record(_audit_row(action="auth.failure", outcome="failure"))
    assert failed["EventResult"] == "Failure" and failed["EventSeverity"] == "Medium"
    denied = se.audit_record(_audit_row(outcome="failure"))
    assert denied["EventSeverity"] == "Low"


def test_size_batches_stay_under_the_limit():
    records = [{"EventUid": str(i), "x": "y" * 100} for i in range(50)]
    batches = se.size_batches(records, max_bytes=1000)
    assert sum(len(b) for b in batches) == 50 and len(batches) > 1
    for b in batches:
        assert len(b"[" + b",".join(se._dumps(r) for r in b) + b"]") <= 1000
    with pytest.raises(ConnectorError):
        se.size_batches([{"x": "y" * 2000}], max_bytes=1000)


def test_call_cursor_round_trip_and_rejects_garbage():
    ts = datetime(2026, 11, 1, 9, 0, 0, 123456, tzinfo=UTC)
    eid = uuid.uuid4()
    assert se.decode_call_cursor(se.encode_call_cursor(ts, eid)) == (ts, eid)
    for bad in ("", "not-a-cursor", "MjAyNg", se.encode_call_cursor(ts, eid)[:-4]):
        with pytest.raises(ValueError):
            se.decode_call_cursor(bad)


# ── schema docs ─────────────────────────────────────────────────────────


def test_docs_table_and_dcr_match_the_code():
    assert json.loads((DOCS / "table.json").read_text()) == se.table_definition()
    assert json.loads((DOCS / "dcr.json").read_text()) == se.dcr_definition()


def test_schema_is_a_valid_custom_table():
    names = [c[0] for c in se.COLUMNS]
    assert names[0] == "TimeGenerated" and len(names) == len(set(names))
    assert se.TABLE_NAME.endswith("_CL")
    allowed = {"string", "int", "long", "real", "boolean", "datetime", "dynamic", "guid"}
    assert {c[1] for c in se.COLUMNS} <= allowed


def test_scopes():
    assert "evidence:read" in SCOPES
    assert "evidence:read" not in LEGACY_REGISTRY_READ
    assert "evidence:read" not in expand_scopes(["registry:read"])


# ── configuration ───────────────────────────────────────────────────────


def test_connection_is_validated():
    conn = se.connection_for_tenant(TENANT, _settings())
    assert conn.ingestion_host == INGEST_HOST
    assert conn.token_scope == "https://monitor.azure.com//.default"
    assert se.connection_for_tenant(uuid.uuid4(), _settings()) is None
    # A host outside the cloud's ingestion suffix is refused.
    for endpoint in (
        "https://evil.example.com",
        "https://ingest.monitor.azure.com",
        "https://x.westeurope-1.ingest.monitor.azure.us",
    ):
        with pytest.raises(ConnectorError):
            se.connection_for_tenant(TENANT, _settings(ingestion_endpoint=endpoint))
    gov = se.connection_for_tenant(
        TENANT,
        _settings(cloud="usgov", ingestion_endpoint="https://x.usgovvirginia-1.ingest.monitor.azure.us"),
    )
    assert gov.token_host == "login.microsoftonline.us"


@pytest.mark.parametrize(
    "over",
    [
        {"ingestion_endpoint": f"http://{INGEST_HOST}"},
        {"ingestion_endpoint": f"https://{INGEST_HOST}/dataCollectionRules"},
        {"ingestion_endpoint": f"https://user:pw@{INGEST_HOST}"},
        {"ingestion_endpoint": f"https://{INGEST_HOST}:8443"},
        {"dcr_immutable_id": "dcr-../../x"},
        {"client_id": "not-a-guid"},
        {"entra_tenant_id": "../common"},
        {"stream_name": "Custom-x/../y"},
        {"include": []},
        {"cloud": "other"},
    ],
)
def test_bad_configuration_is_refused(over):
    with pytest.raises(ValueError):
        _settings(**over)


def test_bad_env_does_not_echo_the_secret(monkeypatch):
    monkeypatch.setenv(
        "PS_SENTINEL_EXPORT", json.dumps({str(TENANT): _conn(client_id="nope")})
    )
    with pytest.raises(ConnectorError) as exc:
        se.load_settings()
    assert SECRET not in str(exc.value)


# ── client against mocked Entra + Logs Ingestion ────────────────────────


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _private(host: str, port: int) -> list[str]:
    return ["10.0.0.5"]


async def _no_sleep(_s: float) -> None:
    return None


class FakeAzure:
    def __init__(self, upload_status: list[int] | None = None):
        self.tokens = 0
        self.uploads: list[list[dict]] = []
        self.requests: list[httpx.Request] = []
        self.upload_status = list(upload_status or [])

    def __call__(self, r: httpx.Request) -> httpx.Response:
        self.requests.append(r)
        assert r.url.host == "93.184.216.34"
        host = r.headers["host"]
        if host == "login.microsoftonline.com":
            assert r.url.path == f"/{ENTRA_TENANT}/oauth2/v2.0/token"
            form = parse_qs(r.content.decode())
            assert form["grant_type"] == ["client_credentials"]
            assert form["client_id"] == [CLIENT_ID]
            assert form["client_secret"] == [SECRET]
            assert form["scope"] == ["https://monitor.azure.com//.default"]
            self.tokens += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens}", "expires_in": 3599}
            )
        assert host == INGEST_HOST
        assert r.url.path == f"/dataCollectionRules/{DCR}/streams/Custom-PromptShieldsAudit"
        assert r.url.params["api-version"] == "2023-01-01"
        assert r.headers["authorization"] == f"Bearer token-{self.tokens}"
        assert r.headers["content-type"] == "application/json"
        status = self.upload_status.pop(0) if self.upload_status else 204
        if status == 204:
            self.uploads.append(json.loads(r.content))
        return httpx.Response(status)


async def test_client_gets_a_token_and_uploads(caplog):
    caplog.set_level(logging.DEBUG)
    fake = FakeAzure()
    conn = se.connection_for_tenant(TENANT, _settings())
    async with se.SentinelClient(
        conn, transport=httpx.MockTransport(fake), resolver=_public, sleep=_no_sleep
    ) as c:
        await c.upload([{"EventUid": "1"}])
        await c.upload([{"EventUid": "2"}])
    assert fake.tokens == 1 and fake.uploads == [[{"EventUid": "1"}], [{"EventUid": "2"}]]
    assert SECRET not in caplog.text and "token-1" not in caplog.text


async def test_client_refreshes_the_token_once_on_401_and_retries_429():
    fake = FakeAzure(upload_status=[401, 429, 204])
    conn = se.connection_for_tenant(TENANT, _settings())
    async with se.SentinelClient(
        conn, transport=httpx.MockTransport(fake), resolver=_public, sleep=_no_sleep
    ) as c:
        await c.upload([{"EventUid": "1"}])
    assert fake.tokens == 2 and fake.uploads == [[{"EventUid": "1"}]]


async def test_client_errors_carry_no_body_or_secret():
    def handler(r: httpx.Request) -> httpx.Response:
        if r.headers["host"] == "login.microsoftonline.com":
            return httpx.Response(401, json={"error": f"bad secret {SECRET}"})
        return httpx.Response(204)

    conn = se.connection_for_tenant(TENANT, _settings())
    async with se.SentinelClient(
        conn, transport=httpx.MockTransport(handler), resolver=_public, sleep=_no_sleep
    ) as c:
        with pytest.raises(ConnectorError) as exc:
            await c.upload([{"EventUid": "1"}])
    assert "HTTP 401" in str(exc.value) and SECRET not in str(exc.value)


async def test_client_refuses_private_addresses():
    calls: list[httpx.Request] = []
    conn = se.connection_for_tenant(TENANT, _settings())
    async with se.SentinelClient(
        conn,
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(204)),
        resolver=_private,
        sleep=_no_sleep,
    ) as c:
        with pytest.raises(ConnectorError, match="non-public"):
            await c.upload([{"EventUid": "1"}])
    assert calls == []


def test_audit_record_lets_the_siem_recompute_row_hash():
    for over in ({}, {"details": None}, {"created_at": datetime(2026, 11, 1, 9, 5, 0, 120, tzinfo=UTC)}):
        row = _audit_row(**over)
        rec = se.audit_record(row)
        fields = {
            "id": rec["EventUid"],
            "tenant_id": rec["PsTenantId"],
            "seq": rec["PsAuditSeq"],
            "created_at": rec["TimeGenerated"],
            "action": rec["Operation"],
            "outcome": rec["EventResult"].lower(),
            "actor_type": rec.get("ActorUserType"),
            "actor_id": rec.get("ActorUserId"),
            "target_type": rec.get("ObjectType"),
            "target_id": rec.get("Object"),
            "request_id": rec.get("PsRequestId"),
            "status_code": rec.get("HttpStatusCode"),
            "details": rec.get("PsAuditDetails"),
        }
        assert audit_chain.compute_row_hash(rec["PsAuditPrevHash"], fields) == rec["PsAuditRowHash"]


def test_ingestion_endpoint_refuses_ip_literals_cleanly():
    for ep in ("https://[::1]", "https://10.0.0.1", "https://[2001:db8::1]"):
        with pytest.raises(ValidationError):
            se.SentinelConnection(**_conn(ingestion_endpoint=ep))


@pytest.mark.parametrize(
    ("statuses", "code"),
    [(["ok", "not_configured", "ok"], 0), (["ok", "error", "ok"], 1)],
)
def test_all_tenants_cli_summarises_and_fails_if_any_tenant_failed(
    monkeypatch, capsys, statuses, code
):
    results = [
        {"tenant_id": f"t{i}", "status": s, **({"error": "boom"} if s == "error" else {})}
        for i, s in enumerate(statuses)
    ]

    async def fake_main(tenant, all_tenants):
        assert all_tenants and tenant is None
        return results

    monkeypatch.setattr(se, "_main", fake_main)
    assert se.main(["--all-tenants"]) == code
    out = capsys.readouterr()
    assert json.loads(out.out) == results
    if code:
        assert "sentinel export: 3 tenants, 2 ok, 1 failed: t1 (boom)" in out.err
    else:
        assert "sentinel export: 3 tenants, 3 ok" in out.err
