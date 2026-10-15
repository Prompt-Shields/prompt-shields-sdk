"""LeanIX connector and shared connector plumbing. Partner API and LeanIX
are mocked with httpx.MockTransport; no network, no DB."""

import json
import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from collector import connectors as cx
from collector import leanix as lx
from db.models import Tenant

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
LX_TOKEN = "lx-API-TOKEN-secret-value"
PARTNER_KEY = "ps-partner-KEY-secret-value"
BEARER = "lx-bearer-access-token"
HOST = "acme.leanix.net"
PARTNER = "https://collector.example.com"
A1 = "0190a000-0000-7000-8000-000000000001"
A2 = "0190a000-0000-7000-8000-000000000002"
F1 = "0190a000-0000-7000-8000-0000000000f1"


async def _no_sleep(_s: float) -> None:
    return None


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _private(host: str, port: int) -> list[str]:
    return ["10.0.0.5"]


def _asset(aid=A1, **kw) -> dict:
    a = {
        "id": aid,
        "vendor": "openai",
        "model": "gpt-4o",
        "use_case_name": "Support bot",
        "business_unit": "Customer Care",
        "owner_email": "owner@example.com",
        "environment": "production",
        "region": "eu-west-1",
        "status": "active",
        "data_classification": "confidential",
        "confidence": "high",
        "first_seen": "2026-01-02T03:04:05+00:00",
        "last_seen": "2026-10-01T00:00:00+00:00",
        "lifecycle_state": "active",
        "lifecycle_changed_at": None,
    }
    a.update(kw)
    return a


def _flow(fid=F1) -> dict:
    return {
        "id": fid,
        "source_system": "crm",
        "destination_system": "openai/gpt-4o",
        "data_classification": "confidential",
        "direction": "input",
        "detected_pii_types": ["email"],
    }


def _settings(**kw) -> lx.LeanIXSettings:
    conn = {
        "workspace_host": HOST,
        "api_token": LX_TOKEN,
        "partner_api": {"base_url": PARTNER, "api_key": PARTNER_KEY},
    }
    conn.update(kw)
    return lx.LeanIXSettings(leanix={str(TENANT): conn})


class FakeSession:
    def __init__(self, settings=None):
        self.tenant = Tenant(id=TENANT, name="t", settings=settings or {})
        self.commits = 0

    async def get(self, model, ident):
        return self.tenant if ident == TENANT else None

    async def commit(self):
        self.commits += 1


# ──────────────────────────────────────────────────────────────────────
# Host validation / SSRF
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "169.254.169.254",
        "10.0.0.1",
        "localhost",
        "evil.com",
        "acme.leanix.net.evil.com",
        "user@acme.leanix.net",
        "acme.leanix.net:8443",
        "acme.leanix.net/x",
        "",
    ],
)
def test_workspace_host_rejected(host):
    with pytest.raises(ValueError):
        cx.validate_hostname(host, (".leanix.net",))


def test_workspace_host_accepted_and_normalised():
    assert cx.validate_hostname("ACME.LeanIX.net.", (".leanix.net",)) == HOST
    assert cx.validate_hostname("eu.leanix.net", ("leanix.net",)) == "eu.leanix.net"


@pytest.mark.parametrize(
    "ip,public",
    [
        ("93.184.216.34", True),
        ("10.1.2.3", False),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("::1", False),
        ("::ffff:10.0.0.1", False),
        ("fd00::1", False),
        ("100.64.0.1", False),
        ("2606:4700::1111", True),
        ("0.0.0.0", False),
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 -> 169.254.169.254
        ("::a9fe:a9fe", False),  # IPv4-compatible
        ("fec0::1", False),  # site-local
        ("2002:a00:1::", False),  # 6to4 of 10.0.0.1
        ("fe80::1%eth0", False),
        ("not-an-ip", False),
    ],
)
def test_is_public_ip(ip, public):
    assert cx.is_public_ip(ip) is public


def test_connection_refuses_host_outside_allowed_suffixes():
    with pytest.raises(cx.ConnectorError) as exc:
        lx.connection_for_tenant(TENANT, _settings(workspace_host="metadata.internal"))
    assert LX_TOKEN not in str(exc.value)


def test_empty_allowed_suffixes_fail_closed():
    settings = _settings(workspace_host="metadata.internal")
    settings.leanix_allowed_host_suffixes = []
    with pytest.raises(cx.ConnectorError, match="empty"):
        lx.connection_for_tenant(TENANT, settings)
    settings.leanix_allowed_host_suffixes = ["."]
    with pytest.raises(cx.ConnectorError, match="empty"):
        lx.connection_for_tenant(TENANT, settings)


def test_partner_api_must_be_https():
    with pytest.raises(ValueError):
        cx.PartnerApiConnection(base_url="http://collector", api_key="k")
    with pytest.raises(ValueError):
        cx.PartnerApiConnection(base_url="https://u:p@collector", api_key="k")


def test_malformed_env_does_not_echo_token(monkeypatch):
    monkeypatch.setenv("PS_LEANIX", json.dumps({str(TENANT): {"api_token": LX_TOKEN}}))
    with pytest.raises(cx.ConnectorError) as exc:
        lx.connection_for_tenant(TENANT)
    assert LX_TOKEN not in str(exc.value)


async def test_guarded_client_refuses_private_resolution():
    calls = []
    transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200))
    async with cx.GuardedClient(
        f"https://{HOST}", name="t", transport=transport, resolver=_private
    ) as c:
        with pytest.raises(cx.ConnectorError, match="non-public"):
            await c.request("GET", "/x", what="x")
    assert calls == []


async def test_guarded_client_pins_connection_to_checked_address():
    """DNS rebinding: the name is resolved once per request and the
    connection goes to that checked address, so a second lookup returning
    a private address is never used."""
    answers = [["93.184.216.34"], ["169.254.169.254"]]
    lookups, seen = [], []

    async def rebinding(host, port):
        lookups.append((host, port))
        return answers[min(len(lookups) - 1, 1)]

    def handler(r):
        seen.append(r)
        return httpx.Response(200)

    async with cx.GuardedClient(
        f"https://{HOST}",
        name="t",
        transport=httpx.MockTransport(handler),
        resolver=rebinding,
    ) as c:
        await c.request("GET", "/x", what="x", headers={"X-A": "1"})
        with pytest.raises(cx.ConnectorError, match="non-public"):
            await c.request("GET", "/x", what="x")
    assert lookups == [(HOST, 443), (HOST, 443)]
    assert len(seen) == 1
    r = seen[0]
    assert str(r.url) == "https://93.184.216.34/x"
    assert r.headers["host"] == HOST and r.headers["x-a"] == "1"
    assert r.extensions["sni_hostname"] == HOST


async def test_guarded_client_pins_ipv6_and_private_opt_in():
    seen = []

    async def v6(host, port):
        return ["2606:4700::1111"]

    async def internal(host, port):
        return ["10.0.0.7"]

    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200))
    async with cx.GuardedClient(
        f"https://{HOST}:8443", name="t", transport=transport, resolver=v6
    ) as c:
        await c.request("GET", "/x", what="x")
    async with cx.GuardedClient(
        "https://collector.internal",
        name="t",
        transport=transport,
        resolver=internal,
        allow_private_network=True,
    ) as c:
        await c.request("GET", "/x", what="x")
    assert str(seen[0].url) == "https://[2606:4700::1111]:8443/x"
    assert seen[0].headers["host"] == f"{HOST}:8443"
    # Private opt-in still connects to the address it checked.
    assert seen[1].url.host == "10.0.0.7"
    assert seen[1].extensions["sni_hostname"] == "collector.internal"


async def test_guarded_client_does_not_follow_redirects():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    async with cx.GuardedClient(
        f"https://{HOST}", name="t", transport=transport, resolver=_public
    ) as c:
        with pytest.raises(cx.ConnectorError, match="HTTP 302"):
            await c.request("GET", "/x", what="x")


async def test_guarded_client_retries_are_bounded_and_honour_retry_after():
    waits, calls = [], []

    async def sleep(s):
        waits.append(s)

    def handler(r):
        calls.append(r)
        return httpx.Response(429, headers={"retry-after": "7"}, text=LX_TOKEN)

    async with cx.GuardedClient(
        f"https://{HOST}",
        name="t",
        transport=httpx.MockTransport(handler),
        resolver=_public,
        sleep=sleep,
        max_retries=2,
    ) as c:
        with pytest.raises(cx.ConnectorError) as exc:
            await c.request("GET", "/x", what="x")
    assert len(calls) == 3 and waits == [7.0, 7.0]
    assert LX_TOKEN not in str(exc.value)


async def test_guarded_client_error_never_echoes_body():
    transport = httpx.MockTransport(lambda r: httpx.Response(401, text=LX_TOKEN))
    async with cx.GuardedClient(
        f"https://{HOST}", name="t", transport=transport, resolver=_public
    ) as c:
        with pytest.raises(cx.ConnectorError) as exc:
            await c.request("GET", "/x", what="x")
    assert str(exc.value) == "t: x returned HTTP 401"


def test_guarded_client_requires_https():
    with pytest.raises(cx.ConnectorError):
        cx.GuardedClient(f"http://{HOST}", name="t")


# ──────────────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────────────


def test_build_ldif_maps_assets_vendors_business_units_and_flows():
    ldif = lx.build_ldif(
        [_asset(), _asset(A2, use_case_name=None, business_unit=None)],
        {A1: [_flow()]},
        connector_id="prompt-shields",
    )
    assert ldif["processingMode"] == "partial"
    by_id = {(i["type"], i["id"]): i for i in ldif["content"]}
    assert set(by_id) == {
        ("ITComponent", f"promptshields:asset:{A1}"),
        ("ITComponent", f"promptshields:asset:{A2}"),
        ("Provider", "promptshields:provider:openai"),  # once, shared
        ("Organization", "promptshields:business-unit:Customer Care"),
        ("Interface", f"promptshields:flow:{F1}"),
    }
    itc = by_id[("ITComponent", f"promptshields:asset:{A1}")]["data"]
    assert itc["name"] == "Support bot [0190a000]"
    assert itc["providerId"] == "promptshields:provider:openai"
    assert itc["lifecyclePhase"] == "active" and itc["lifecycleStart"] == "2026-01-02"
    assert "Business unit: Customer Care." in itc["description"]
    assert by_id[("ITComponent", f"promptshields:asset:{A2}")]["data"]["name"] == (
        "openai gpt-4o [0190a000]"
    )
    iface = by_id[("Interface", f"promptshields:flow:{F1}")]["data"]
    assert iface["itComponentId"] == f"promptshields:asset:{A1}"
    assert "email" in iface["description"]


def test_external_ids_match_aibom_bom_refs():
    from collector import aibom

    assert lx.asset_external_id(A1) == f"{aibom.NS}:asset:{A1}"
    assert lx.provider_external_id("openai") == aibom._provider_ref("openai")


def test_retired_asset_is_end_of_life():
    item = lx.asset_items(
        _asset(
            lifecycle_state="retired", lifecycle_changed_at="2026-09-30T10:00:00+00:00"
        )
    )[0]
    assert item["data"]["lifecyclePhase"] == "endOfLife"
    assert item["data"]["lifecycleStart"] == "2026-09-30"


def test_processor_configuration_covers_every_ldif_type():
    cfg = lx.processor_configuration("prompt-shields")
    types = {
        p["type"] for p in cfg["processors"] if p["processorType"] == "inboundFactSheet"
    }
    assert types == {"ITComponent", "Provider", "Organization", "Interface"}
    for p in cfg["processors"]:
        ident = p.get("identifier") or p["from"]
        assert ident["external"]["type"]["expr"] == "externalId"
    assert cfg["connectorId"] == "prompt-shields"


def test_business_unit_fact_sheet_type_is_configurable():
    ldif = lx.build_ldif(
        [_asset()], connector_id="prompt-shields", business_unit_type="UserGroup"
    )
    types = {i["type"] for i in ldif["content"]}
    assert "UserGroup" in types and "Organization" not in types
    cfg = lx.processor_configuration("prompt-shields", "UserGroup")
    bu = [p for p in cfg["processors"] if p["type"] == "UserGroup"]
    assert len(bu) == 1 and bu[0]["filter"] == {"exactType": "UserGroup"}
    with pytest.raises(ValueError):
        _settings(business_unit_type="Team")


def test_lx_workspace_is_optional_and_sent_when_set():
    ws = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert "lxWorkspace" not in lx.build_ldif([_asset()], connector_id="c")
    ldif = lx.build_ldif([_asset()], connector_id="c", lx_workspace=ws)
    assert ldif["lxWorkspace"] == str(ws)
    with pytest.raises(ValueError):
        _settings(lx_workspace="not-a-uuid")


@pytest.mark.parametrize("value", [12345, 1.5, ["2026"], {"a": 1}, True, "nope"])
def test_malformed_dates_are_dropped_not_raised(value):
    data = lx.asset_items(_asset(first_seen=value))[0]["data"]
    assert "lifecycleStart" not in data
    retired = lx.asset_items(
        _asset(lifecycle_state="retired", lifecycle_changed_at=value, last_seen=value)
    )[0]["data"]
    assert "lifecycleStart" not in retired
    assert cx.next_cursor("2026-10-15T00:00:00+00:00", [{"changed_at": value}], None)
    assert cx.next_cursor(None, [], value) is None


def test_next_cursor():
    assert (
        lx.next_cursor(None, [], "2026-10-15T12:00:00Z") == "2026-10-15T11:55:00+00:00"
    )
    since = "2026-10-15T00:00:00+00:00"
    assert lx.next_cursor(since, [], None) == since
    changes = [{"changed_at": "2026-10-15T12:00:00+00:00"}, {"changed_at": None}]
    assert lx.next_cursor(since, changes, None) == "2026-10-15T11:55:00+00:00"
    # Never moves back before `since`.
    early = [{"changed_at": "2026-10-15T00:01:00+00:00"}]
    assert lx.next_cursor(since, early, None) == since


# ──────────────────────────────────────────────────────────────────────
# Sync job against mocked Partner API and LeanIX
# ──────────────────────────────────────────────────────────────────────


class Fakes:
    def __init__(
        self, assets=None, changes=None, run_status=("IN_PROGRESS", "FINISHED")
    ):
        self.assets = assets if assets is not None else [_asset()]
        self.changes = changes or []
        self.run_status = list(run_status)
        self.partner_requests: list[httpx.Request] = []
        self.leanix_requests: list[httpx.Request] = []
        self.ldifs: list[dict] = []
        self.fail_leanix_with: int | None = None
        # Tenant of the partner key, as the Partner API reports it.
        self.key_tenant: str | None = str(TENANT)

    def partner(self, r: httpx.Request) -> httpx.Response:
        self.partner_requests.append(r)
        # Connected to the checked address; Host and SNI keep the name.
        assert r.url.host == "93.184.216.34" and r.url.scheme == "https"
        assert r.headers["host"] == "collector.example.com"
        assert r.extensions["sni_hostname"] == "collector.example.com"
        assert r.headers["authorization"] == f"Bearer {PARTNER_KEY}"
        tenant = {"tenant_id": self.key_tenant}
        if r.url.path == "/partner/v1/export":
            return httpx.Response(
                200,
                json={
                    "data": self.assets,
                    "meta": {"sync_token": "2026-10-15T12:00:00Z", **tenant},
                },
            )
        if r.url.path == "/partner/v1/changes":
            return httpx.Response(
                200,
                json={"data": self.changes, "meta": {"has_more": False, **tenant}},
            )
        if r.url.path.endswith("/data-flows"):
            flows = [_flow()] if f"/assets/{A1}/" in r.url.path else []
            return httpx.Response(200, json={"data": flows, "meta": tenant})
        return httpx.Response(404)

    def leanix(self, r: httpx.Request) -> httpx.Response:
        self.leanix_requests.append(r)
        assert r.url.host == "93.184.216.34" and r.url.scheme == "https"
        assert r.headers["host"] == HOST and r.extensions["sni_hostname"] == HOST
        assert PARTNER_KEY not in r.headers.get("authorization", "")
        if r.url.path == lx.TOKEN_PATH:
            assert r.headers["authorization"].startswith("Basic ")
            return httpx.Response(
                200, json={"access_token": BEARER, "expires_in": 3600}
            )
        assert r.headers["authorization"] == f"Bearer {BEARER}"
        if self.fail_leanix_with:
            return httpx.Response(self.fail_leanix_with, text=LX_TOKEN)
        if r.url.path == f"{lx.INTEGRATION_API}/configurations/":
            assert r.method == "PUT"
            return httpx.Response(200, json={})
        if r.url.path == f"{lx.INTEGRATION_API}/synchronizationRuns":
            assert r.url.params["start"] == "true"
            self.ldifs.append(json.loads(r.content))
            return httpx.Response(200, json={"id": f"run-{len(self.ldifs)}"})
        if r.url.path.endswith("/status"):
            status = (
                self.run_status.pop(0)
                if len(self.run_status) > 1
                else self.run_status[0]
            )
            return httpx.Response(200, json={"status": status})
        return httpx.Response(404)

    def kwargs(self, settings=None):
        return {
            "settings": settings or _settings(),
            "partner_transport": httpx.MockTransport(self.partner),
            "leanix_transport": httpx.MockTransport(self.leanix),
            "resolver": _public,
            "sleep": _no_sleep,
            "now": datetime(2026, 10, 15, 12, 30, tzinfo=UTC),
        }


async def test_full_then_incremental_sync(caplog):
    caplog.set_level(logging.DEBUG)
    db = FakeSession()
    fakes = Fakes()
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok", out
    assert out["mode"] == "full" and out["assets"] == 1 and out["data_flows"] == 1
    assert out["run_ids"] == ["run-1"]
    assert next(r.url.path for r in fakes.partner_requests) == "/partner/v1/export"
    ldif = fakes.ldifs[0]
    assert {i["type"] for i in ldif["content"]} == {
        "ITComponent",
        "Provider",
        "Organization",
        "Interface",
    }
    state = db.tenant.settings["connectors"]["leanix"]
    assert state["cursor"] == "2026-10-15T11:55:00+00:00"
    assert state["last_status"] == "ok"

    # Incremental: reads /changes since the stored cursor.
    fakes2 = Fakes(
        changes=[
            {
                "change_type": "updated",
                "asset": _asset(A2),
                "changed_at": "2026-10-15T13:00:00+00:00",
            }
        ]
    )
    out = await lx.sync_leanix(db, TENANT, **fakes2.kwargs())
    assert out["status"] == "ok" and out["mode"] == "incremental"
    changes_req = fakes2.partner_requests[0]
    assert changes_req.url.path == "/partner/v1/changes"
    assert changes_req.url.params["since"] == "2026-10-15T11:55:00+00:00"
    assert db.tenant.settings["connectors"]["leanix"]["cursor"] == (
        "2026-10-15T12:55:00+00:00"
    )

    blob = json.dumps(db.tenant.settings) + json.dumps(out) + caplog.text
    for secret in (LX_TOKEN, PARTNER_KEY, BEARER):
        assert secret not in blob


async def test_no_changes_skips_leanix_and_keeps_cursor():
    db = FakeSession(
        {"connectors": {"leanix": {"cursor": "2026-10-15T00:00:00+00:00"}}}
    )
    fakes = Fakes()
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok" and out["runs"] == 0
    assert fakes.leanix_requests == []
    assert (
        db.tenant.settings["connectors"]["leanix"]["cursor"]
        == "2026-10-15T00:00:00+00:00"
    )


async def test_batches_split_into_several_runs():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2)], run_status=("FINISHED",))
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs(_settings(batch_size=1)))
    assert out["runs"] == 2 and len(fakes.ldifs) == 2
    # Each run carries the provider its IT Component relates to.
    for ldif in fakes.ldifs:
        assert any(i["type"] == "Provider" for i in ldif["content"])


async def test_failed_run_records_error_and_keeps_cursor():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"leanix": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}],
        run_status=("FAILED",),
    )
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error" and "run-1 FAILED" in out["error"]
    state = db.tenant.settings["connectors"]["leanix"]
    assert state["cursor"] == cursor and state["last_status"] == "error"


async def test_leanix_http_error_does_not_leak():
    db = FakeSession()
    fakes = Fakes()
    fakes.fail_leanix_with = 403
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error"
    assert out["error"] == "leanix: processor configuration returned HTTP 403"
    assert LX_TOKEN not in json.dumps(db.tenant.settings)


async def test_ssrf_guard_blocks_before_any_request():
    db = FakeSession()
    fakes = Fakes()
    kw = fakes.kwargs()
    kw["resolver"] = _private
    out = await lx.sync_leanix(db, TENANT, **kw)
    assert out["status"] == "error" and "non-public" in out["error"]
    assert fakes.partner_requests == [] and fakes.leanix_requests == []


@pytest.mark.parametrize("key_tenant", [str(uuid.uuid4()), None])
async def test_partner_key_of_another_tenant_fails_closed(key_tenant):
    """A partner key paired with the wrong tenant in PS_LEANIX (or a Partner
    API that does not say whose key it is) stops the run before LeanIX."""
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"leanix": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.key_tenant = key_tenant
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert fakes.leanix_requests == []
    state = db.tenant.settings["connectors"]["leanix"]
    assert state["cursor"] == cursor and state["last_status"] == "error"
    if key_tenant:
        assert key_tenant not in out["error"]


async def test_not_configured_and_unknown_tenant():
    db = FakeSession()
    out = await lx.sync_leanix(db, TENANT, settings=lx.LeanIXSettings())
    assert out["status"] == "not_configured"
    out = await lx.sync_leanix(db, uuid.uuid4(), settings=_settings())
    assert out["status"] == "tenant_not_found"


async def test_invalid_run_id_is_refused():
    db = FakeSession()
    fakes = Fakes()
    orig = fakes.leanix

    def handler(r):
        if r.url.path == f"{lx.INTEGRATION_API}/synchronizationRuns":
            return httpx.Response(200, json={"id": "../../configurations"})
        return orig(r)

    kw = fakes.kwargs()
    kw["leanix_transport"] = httpx.MockTransport(handler)
    out = await lx.sync_leanix(db, TENANT, **kw)
    assert out["status"] == "error" and "no valid id" in out["error"]


async def test_sync_sends_configured_workspace_and_business_unit_type():
    ws = "22222222-2222-2222-2222-222222222222"
    db = FakeSession()
    fakes = Fakes()
    settings = _settings(lx_workspace=ws, business_unit_type="UserGroup")
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    assert fakes.ldifs[0]["lxWorkspace"] == ws
    assert "UserGroup" in {i["type"] for i in fakes.ldifs[0]["content"]}
    cfg = json.loads(fakes.leanix_requests[1].content)
    assert "UserGroup" in {p["type"] for p in cfg["processors"]}


async def test_malformed_partner_dates_do_not_fail_the_sync():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(first_seen=12345, last_seen={"x": 1})])
    out = await lx.sync_leanix(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok", out


# ──────────────────────────────────────────────────────────────────────
# Token refresh
# ──────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def test_long_sync_refreshes_token_before_expiry_and_after_401(caplog):
    """A run polled past the token lifetime refreshes it proactively; a 401
    anyway refreshes it once more and repeats the call once."""
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    issued: list[str] = []
    seen: list[tuple[str, str]] = []
    reject: set[str] = set()

    async def tick(_s: float) -> None:
        clock.t += 1000  # each poll interval "lasts" 1000s

    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == lx.TOKEN_PATH:
            issued.append(f"{BEARER}-{len(issued) + 1}")
            return httpx.Response(
                200, json={"access_token": issued[-1], "expires_in": "3600"}
            )
        auth = r.headers["authorization"].removeprefix("Bearer ")
        seen.append((r.url.path.rsplit("/", 1)[-1], auth))
        if auth in reject:
            return httpx.Response(401, text=auth)
        if r.url.path.endswith("/status"):
            done = sum(p == "status" for p, _ in seen) >= 5
            return httpx.Response(
                200, json={"status": "FINISHED" if done else "RUNNING"}
            )
        return httpx.Response(200, json={"id": "run-1"})

    conn = lx.connection_for_tenant(TENANT, _settings())
    async with lx.LeanIXClient(
        conn,
        transport=httpx.MockTransport(handler),
        resolver=_public,
        sleep=tick,
        clock=clock,
    ) as client:
        run_id = await client.start_run({"content": []})
        assert await client.wait_for_run(run_id) == "FINISHED"
        # Server revokes the current token early: one refresh, one repeat.
        reject.add(issued[-1])
        assert await client.start_run({"content": []}) == "run-1"

    # Polls at +0..+3000s use token 1 (refresh due at +3540s); +4000s token 2.
    assert [a for p, a in seen if p == "status"] == [
        f"{BEARER}-1",
        f"{BEARER}-1",
        f"{BEARER}-1",
        f"{BEARER}-1",
        f"{BEARER}-2",
    ]
    assert seen[-2:] == [
        ("synchronizationRuns", f"{BEARER}-2"),
        ("synchronizationRuns", f"{BEARER}-3"),
    ]
    assert len(issued) == 3
    for token in issued + [LX_TOKEN]:
        assert token not in caplog.text


async def test_repeated_401_fails_once_refreshed():
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.path)
        if r.url.path == lx.TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": BEARER, "expires_in": 3600}
            )
        return httpx.Response(401)

    conn = lx.connection_for_tenant(TENANT, _settings())
    async with lx.LeanIXClient(
        conn, transport=httpx.MockTransport(handler), resolver=_public, sleep=_no_sleep
    ) as client:
        with pytest.raises(cx.ConnectorError, match="returned HTTP 401"):
            await client.put_processor_configuration()
    assert calls.count(lx.TOKEN_PATH) == 2 and len(calls) == 4


@pytest.mark.parametrize(
    "value,expected",
    [
        (3600, 3600.0),
        ("90", 90.0),
        (None, None),
        (0, None),
        (-5, None),
        ("soon", None),
        (True, None),
        (float("inf"), None),
        (float("nan"), None),
    ],
)
def test_expires_in_parsing(value, expected):
    assert cx._expires_in(value) == expected
