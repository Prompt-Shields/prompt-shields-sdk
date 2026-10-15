"""ServiceNow connector. Partner API and ServiceNow (OAuth token endpoint
and the IRE API) are mocked with httpx.MockTransport; no network, no DB."""

import base64
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from collector import servicenow as sn
from db.models import Tenant

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
CLIENT_SECRET = "sn-CLIENT-SECRET-value"
PASSWORD = "sn-PASSWORD-value"
PARTNER_KEY = "ps-partner-KEY-secret-value"
BEARER = "sn-bearer-access-token"
HOST = "acme.service-now.com"
PARTNER = "https://collector.example.com"
A1 = "0190a000-0000-7000-8000-000000000001"
A2 = "0190a000-0000-7000-8000-000000000002"
A3 = "0190a000-0000-7000-8000-000000000003"
SECRETS = (CLIENT_SECRET, PASSWORD, PARTNER_KEY, BEARER)


async def _no_sleep(_s: float) -> None:
    return None


async def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


async def _private(host: str, port: int) -> list[str]:
    return ["169.254.169.254"]


def _asset(aid=A1, **kw) -> dict:
    a = {
        "id": aid,
        "vendor": "openai",
        "model": "gpt-4o",
        "use_case_name": "Support bot",
        "business_unit": "Customer Care",
        "owner_email": "owner@example.com",
        "environment": "production",
        "data_classification": "confidential",
        "confidence": "high",
        "first_seen": "2026-01-02T03:04:05+00:00",
        "last_seen": "2026-10-01T10:20:30+02:00",
        "lifecycle_state": "active",
    }
    a.update(kw)
    return a


def _conn(**kw) -> dict:
    conn = {
        "instance_host": HOST,
        "oauth_client_id": "ps-connector",
        "oauth_client_secret": CLIENT_SECRET,
        "partner_api": {"base_url": PARTNER, "api_key": PARTNER_KEY},
    }
    conn.update(kw)
    return conn


def _settings(**kw) -> sn.ServiceNowSettings:
    return sn.ServiceNowSettings(servicenow={str(TENANT): _conn(**kw)})


def _connection(**kw) -> sn.ServiceNowConnection:
    return sn.ServiceNowConnection(**_conn(**kw))


class FakeSession:
    def __init__(self, settings=None):
        self.tenant = Tenant(id=TENANT, name="t", settings=settings or {})
        self.commits = 0

    async def get(self, model, ident):
        return self.tenant if ident == TENANT else None

    async def commit(self):
        self.commits += 1


# ──────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    ["169.254.169.254", "evil.com", "acme.service-now.com.evil.com", "acme.leanix.net"],
)
def test_instance_host_outside_allowed_suffixes_is_refused(host):
    with pytest.raises(sn.ConnectorError, match="instance_host"):
        sn.connection_for_tenant(TENANT, _settings(instance_host=host))


def test_instance_host_normalised():
    conn = sn.connection_for_tenant(
        TENANT, _settings(instance_host="ACME.Service-Now.com.")
    )
    assert conn.instance_host == HOST and conn.auth_method == "oauth_client_credentials"


def test_empty_allowed_suffixes_fail_closed():
    s = sn.ServiceNowSettings(
        servicenow={str(TENANT): _conn()}, servicenow_allowed_host_suffixes=[]
    )
    with pytest.raises(sn.ConnectorError, match="empty"):
        sn.connection_for_tenant(TENANT, s)


@pytest.mark.parametrize(
    "auth",
    [
        {"oauth_client_id": None, "oauth_client_secret": None},  # none
        {"username": "u", "password": PASSWORD},  # both
        {"oauth_client_secret": None},  # half of OAuth
    ],
)
def test_exactly_one_auth_method(auth):
    with pytest.raises(ValueError):
        _connection(**auth)


@pytest.mark.parametrize(
    "field,value",
    [
        ("asset_class", "cmdb_ci; drop"),
        ("provider_class", "../sys_user"),
        ("data_source", "x" * 41),
        ("batch_size", 0),
    ],
)
def test_invalid_connection_fields(field, value):
    with pytest.raises(ValueError):
        _connection(**{field: value})


def test_malformed_env_does_not_echo_secret(monkeypatch):
    monkeypatch.setenv(
        "PS_SERVICENOW", json.dumps({str(TENANT): _conn(instance_host=None)})
    )
    with pytest.raises(sn.ConnectorError) as exc:
        sn.connection_for_tenant(TENANT)
    assert CLIENT_SECRET not in str(exc.value)


# ──────────────────────────────────────────────────────────────────────
# Mapping
# ──────────────────────────────────────────────────────────────────────


def test_payload_maps_assets_vendors_and_relations():
    conn = _connection()
    payload = sn.build_payload(
        [_asset(A1), _asset(A2, vendor="anthropic"), _asset(A3), {"id": "x"}], conn
    )
    items = payload["items"]
    # A1, openai, A2, anthropic, A3 (openai once); the vendorless entry is skipped.
    assert [i["className"] for i in items] == [
        "cmdb_ci_function_ai",
        "cmdb_ci_service",
        "cmdb_ci_function_ai",
        "cmdb_ci_service",
        "cmdb_ci_function_ai",
    ]
    a1 = items[0]
    assert a1["values"]["correlation_id"] == f"promptshields:asset:{A1}"
    assert a1["values"]["name"] == "Support bot [0190a000]"
    assert a1["values"]["install_status"] == "1"
    assert a1["values"]["operational_status"] == "1"
    assert "Owner: owner@example.com." in a1["values"]["short_description"]
    assert a1["sys_object_source_info"] == {
        "source_name": "PromptShields",
        "source_native_key": f"promptshields:asset:{A1}",
        "source_recency_timestamp": "2026-10-01 08:20:30",
    }
    assert items[1]["values"]["correlation_id"] == "promptshields:provider:openai"
    assert payload["relations"] == [
        {"type": "Depends on::Used by", "parent": 0, "child": 1},
        {"type": "Depends on::Used by", "parent": 2, "child": 3},
        {"type": "Depends on::Used by", "parent": 4, "child": 1},
    ]


def test_identifiers_match_aibom_bom_refs():
    from collector import aibom

    assert sn.asset_ref(A1) == f"{aibom.NS}:asset:{A1}"
    assert sn.provider_ref("openai") == f"{aibom.NS}:provider:openai"


@pytest.mark.parametrize(
    "state,install,operational",
    [
        ("active", "1", "1"),
        ("stale", "1", "2"),
        ("retired", "7", "6"),
        (None, "1", "1"),
    ],
)
def test_lifecycle_maps_to_cmdb_status(state, install, operational):
    item = sn.asset_item(_asset(lifecycle_state=state), _connection())
    assert item["values"]["install_status"] == install
    assert item["values"]["operational_status"] == operational


def test_provider_class_none_pushes_assets_only():
    conn = _connection(provider_class=None, asset_class="cmdb_ci_appl")
    payload = sn.build_payload([_asset(A1), _asset(A2)], conn)
    assert [i["className"] for i in payload["items"]] == ["cmdb_ci_appl"] * 2
    assert payload["relations"] == []


def test_self_hosted_vendors_map_to_the_self_hosted_class():
    conn = _connection()
    assert conn.asset_class == "cmdb_ci_function_ai"
    assert conn.self_hosted_asset_class == "cmdb_ci_appl_ai_application"
    payload = sn.build_payload(
        [_asset(A1), _asset(A2, vendor="Ollama"), _asset(A3, vendor="vllm")], conn
    )
    classes = {i["values"]["correlation_id"]: i["className"] for i in payload["items"]}
    assert classes[f"promptshields:asset:{A1}"] == "cmdb_ci_function_ai"
    assert classes[f"promptshields:asset:{A2}"] == "cmdb_ci_appl_ai_application"
    assert classes[f"promptshields:asset:{A3}"] == "cmdb_ci_appl_ai_application"
    # Vendors keep the provider class whatever the hosting.
    assert classes["promptshields:provider:Ollama"] == "cmdb_ci_service"


def test_self_hosted_mapping_is_configurable():
    conn = _connection(
        self_hosted_vendors=[" InHouse "], self_hosted_asset_class="u_ai_app"
    )
    assert conn.class_for("inhouse") == "u_ai_app"
    assert conn.class_for("ollama") == "cmdb_ci_function_ai"
    # None: every asset goes to asset_class.
    conn = _connection(self_hosted_asset_class=None)
    assert conn.class_for("ollama") == "cmdb_ci_function_ai"
    with pytest.raises(ValueError):
        _connection(self_hosted_asset_class="Not A Table")


@pytest.mark.parametrize(
    "last_seen", [12345, 1.5, ["2026"], {"a": 1}, True, "nope", ""]
)
def test_malformed_last_seen_is_dropped_not_raised(last_seen):
    item = sn.asset_item(_asset(last_seen=last_seen), _connection())
    assert "source_recency_timestamp" not in item["sys_object_source_info"]


async def test_malformed_partner_data_is_a_failed_or_ok_run_not_a_500():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(last_seen=12345), "changed_at": 1700000000}]
    )
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok", out
    sent = fakes.payloads[0]["items"][0]["sys_object_source_info"]
    assert "source_recency_timestamp" not in sent
    # Non-string changed_at does not move the cursor.
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == cursor


# ──────────────────────────────────────────────────────────────────────
# IRE result checks
# ──────────────────────────────────────────────────────────────────────

SYS_ID = "0" * 32
SENT = {"items": [{}, {}], "relations": []}


def _ok(op="INSERT"):
    return {"className": "x", "operation": op, "sysId": SYS_ID}


def test_ire_result_counts_operations_and_accepts_string_result():
    body = {"result": {"items": [_ok(), _ok("UPDATE")], "relations": []}}
    assert sn.check_ire_result(body, SENT) == {"INSERT": 1, "UPDATE": 1}
    as_string = {"result": json.dumps(body["result"])}
    assert sn.check_ire_result(as_string, SENT) == {"INSERT": 1, "UPDATE": 1}


@pytest.mark.parametrize(
    "result,match",
    [
        ({"items": [_ok()]}, "answered 1 of 2"),
        (
            {
                "items": [
                    _ok(),
                    {
                        "errors": [
                            {
                                "error": "MISSING_MATCHING_ATTRIBUTES",
                                "message": "owner@example.com secret detail",
                            }
                        ]
                    },
                ]
            },
            r"rejected 1 entries \(MISSING_MATCHING_ATTRIBUTES\)",
        ),
        ({"items": [_ok(), {"sysId": "../../x"}]}, "NO_SYS_ID"),
        (
            {"items": [_ok(), _ok()], "relations": [{"errors": [{"error": "<b>"}]}]},
            r"\(ERROR\)",
        ),
        ("not json", "unexpected body"),
    ],
)
def test_ire_result_errors_fail_without_echoing_messages(result, match):
    with pytest.raises(sn.ConnectorError, match=match) as exc:
        sn.check_ire_result({"result": result}, SENT)
    assert "owner@example.com" not in str(exc.value)


# ──────────────────────────────────────────────────────────────────────
# Sync job against mocked Partner API and ServiceNow
# ──────────────────────────────────────────────────────────────────────


class Fakes:
    """Partner API plus a ServiceNow instance whose IRE upserts CIs keyed on
    (class, correlation_id), like an identification rule on that field."""

    def __init__(self, assets=None, changes=None):
        self.assets = assets if assets is not None else [_asset()]
        self.changes = changes or []
        self.key_tenant: str | None = str(TENANT)
        self.partner_requests: list[httpx.Request] = []
        self.sn_requests: list[httpx.Request] = []
        self.payloads: list[dict] = []
        self.cmdb: dict[tuple[str, str], dict] = {}
        self.rels: set[tuple[str, str, str]] = set()
        self.fail_with: int | None = None
        self.item_error: str | None = None

    def partner(self, r: httpx.Request) -> httpx.Response:
        self.partner_requests.append(r)
        assert r.url.host == "93.184.216.34" and r.url.scheme == "https"
        assert r.headers["host"] == "collector.example.com"
        assert r.headers["authorization"] == f"Bearer {PARTNER_KEY}"
        tenant = {"tenant_id": self.key_tenant}
        if r.url.path == "/partner/v1/export":
            meta = {"sync_token": "2026-10-15T12:00:00Z", **tenant}
            return httpx.Response(200, json={"data": self.assets, "meta": meta})
        if r.url.path == "/partner/v1/changes":
            meta = {"has_more": False, **tenant}
            return httpx.Response(200, json={"data": self.changes, "meta": meta})
        return httpx.Response(404)

    def servicenow(self, r: httpx.Request) -> httpx.Response:
        self.sn_requests.append(r)
        assert r.url.host == "93.184.216.34" and r.url.scheme == "https"
        assert r.headers["host"] == HOST and r.extensions["sni_hostname"] == HOST
        assert PARTNER_KEY not in r.headers.get("authorization", "")
        if r.url.path == sn.TOKEN_PATH:
            form = dict(httpx.QueryParams(r.content.decode()))
            assert form == {
                "grant_type": "client_credentials",
                "client_id": "ps-connector",
                "client_secret": CLIENT_SECRET,
            }
            return httpx.Response(
                200, json={"access_token": BEARER, "expires_in": 1800}
            )
        if self.fail_with:
            return httpx.Response(self.fail_with, text=f"denied {CLIENT_SECRET}")
        assert r.url.path == sn.IRE_PATH and r.method == "POST"
        assert r.url.params["sysparm_data_source"] == "PromptShields"
        auth = r.headers["authorization"]
        assert auth == f"Bearer {BEARER}" or auth.startswith("Basic ")
        payload = json.loads(r.content)
        self.payloads.append(payload)
        return httpx.Response(200, json={"result": self._ire(payload)})

    def _ire(self, payload: dict) -> dict:
        out, sys_ids = [], []
        for item in payload["items"]:
            if self.item_error:
                out.append(
                    {
                        "className": item["className"],
                        "errors": [
                            {"error": self.item_error, "message": json.dumps(item)}
                        ],
                    }
                )
                sys_ids.append(None)
                continue
            key = (item["className"], item["values"]["correlation_id"])
            sys_id = hashlib.md5("|".join(key).encode()).hexdigest()
            if key not in self.cmdb:
                op = "INSERT"
            elif self.cmdb[key] != item["values"]:
                op = "UPDATE"
            else:
                op = "NO_CHANGE"
            self.cmdb[key] = item["values"]
            out.append(
                {"className": item["className"], "operation": op, "sysId": sys_id}
            )
            sys_ids.append(sys_id)
        for rel in payload["relations"]:
            self.rels.add((sys_ids[rel["parent"]], rel["type"], sys_ids[rel["child"]]))
        return {"items": out, "relations": []}

    def kwargs(self, settings=None):
        return {
            "settings": settings or _settings(),
            "partner_transport": httpx.MockTransport(self.partner),
            "servicenow_transport": httpx.MockTransport(self.servicenow),
            "resolver": _public,
            "sleep": _no_sleep,
            "now": datetime(2026, 10, 15, 12, 30, tzinfo=UTC),
        }


async def test_full_then_incremental_sync_is_an_idempotent_upsert(caplog):
    caplog.set_level(logging.DEBUG)
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, vendor="anthropic")])
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok", out
    assert out["mode"] == "full" and out["assets"] == 2
    assert out["ci_items"] == 4 and out["relations"] == 2 and out["requests"] == 1
    assert out["operations"] == {"INSERT": 4}
    assert [r.url.path for r in fakes.partner_requests] == ["/partner/v1/export"]
    assert len(fakes.cmdb) == 4 and len(fakes.rels) == 2
    state = db.tenant.settings["connectors"]["servicenow"]
    assert state["cursor"] == "2026-10-15T11:55:00+00:00"
    assert state["last_status"] == "ok"

    # Re-running a full sync updates the same CIs: nothing new is created.
    out = await sn.sync_servicenow(db, TENANT, full=True, **fakes.kwargs())
    assert out["status"] == "ok" and out["operations"] == {"NO_CHANGE": 4}
    assert len(fakes.cmdb) == 4 and len(fakes.rels) == 2

    # Incremental: /changes since the stored cursor; A1 retired.
    fakes.changes = [
        {
            "change_type": "updated",
            "asset": _asset(A1, lifecycle_state="retired"),
            "changed_at": "2026-10-15T13:00:00+00:00",
        }
    ]
    fakes.partner_requests.clear()
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok" and out["mode"] == "incremental"
    assert fakes.partner_requests[0].url.params["since"] == "2026-10-15T11:55:00+00:00"
    assert out["operations"] == {"UPDATE": 1, "NO_CHANGE": 1}
    retired = fakes.cmdb[("cmdb_ci_function_ai", f"promptshields:asset:{A1}")]
    assert retired["install_status"] == "7" and retired["operational_status"] == "6"
    assert len(fakes.cmdb) == 4
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == (
        "2026-10-15T12:55:00+00:00"
    )

    blob = json.dumps(db.tenant.settings) + json.dumps(out) + caplog.text
    for secret in SECRETS:
        assert secret not in blob
    # One token request for the whole run, not one per IRE call.
    assert sum(r.url.path == sn.TOKEN_PATH for r in fakes.sn_requests) == 3


async def test_basic_auth_sends_no_token_request():
    db = FakeSession()
    fakes = Fakes()
    settings = _settings(
        oauth_client_id=None, oauth_client_secret=None, username="ps", password=PASSWORD
    )
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    assert [r.url.path for r in fakes.sn_requests] == [sn.IRE_PATH]
    expected = base64.b64encode(f"ps:{PASSWORD}".encode()).decode()
    assert fakes.sn_requests[0].headers["authorization"] == f"Basic {expected}"


async def test_batches_split_into_several_requests():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2), _asset(A3)])
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(_settings(batch_size=2)))
    assert out["requests"] == 2 and len(fakes.payloads) == 2
    # Each request carries the vendor its assets relate to.
    for p in fakes.payloads:
        assert any(i["className"] == "cmdb_ci_service" for i in p["items"])
        for rel in p["relations"]:
            assert rel["child"] < len(p["items"])


async def test_no_changes_skips_servicenow_and_keeps_cursor():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes()
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok" and out["requests"] == 0
    assert fakes.sn_requests == []
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == cursor


async def test_rejected_items_record_error_and_keep_cursor():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.item_error = "MISSING_MATCHING_ATTRIBUTES"
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error"
    assert out["error"] == (
        "servicenow: IRE rejected 2 entries (MISSING_MATCHING_ATTRIBUTES)"
    )
    state = db.tenant.settings["connectors"]["servicenow"]
    assert state["cursor"] == cursor and state["last_status"] == "error"
    assert "owner@example.com" not in json.dumps(state)


async def test_http_error_does_not_leak():
    db = FakeSession()
    fakes = Fakes()
    fakes.fail_with = 403
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["error"] == "servicenow: identify and reconcile returned HTTP 403"
    assert CLIENT_SECRET not in json.dumps(db.tenant.settings)


async def test_ssrf_guard_blocks_before_any_request():
    db = FakeSession()
    fakes = Fakes()
    kw = fakes.kwargs()
    kw["resolver"] = _private
    out = await sn.sync_servicenow(db, TENANT, **kw)
    assert out["status"] == "error" and "non-public" in out["error"]
    assert fakes.partner_requests == [] and fakes.sn_requests == []


@pytest.mark.parametrize("key_tenant", [str(uuid.uuid4()), None])
async def test_partner_key_of_another_tenant_fails_closed(key_tenant):
    db = FakeSession()
    fakes = Fakes()
    fakes.key_tenant = key_tenant
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert fakes.sn_requests == []
    state = db.tenant.settings["connectors"]["servicenow"]
    assert "cursor" not in state and state["last_status"] == "error"


async def test_not_configured_and_unknown_tenant():
    db = FakeSession()
    out = await sn.sync_servicenow(db, TENANT, settings=sn.ServiceNowSettings())
    assert out["status"] == "not_configured"
    out = await sn.sync_servicenow(db, uuid.uuid4(), settings=_settings())
    assert out["status"] == "tenant_not_found"


# ──────────────────────────────────────────────────────────────────────
# Token refresh
# ──────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class TokenFakes:
    """ServiceNow issuing numbered tokens; ``reject`` lists tokens the IRE
    endpoint answers 401 to."""

    def __init__(self, expires_in=1800):
        self.expires_in = expires_in
        self.issued = 0
        self.reject: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def handler(self, r: httpx.Request) -> httpx.Response:
        if r.url.path == sn.TOKEN_PATH:
            self.issued += 1
            self.calls.append(("token", ""))
            body = {"access_token": f"{BEARER}-{self.issued}"}
            if self.expires_in is not None:
                body["expires_in"] = self.expires_in
            return httpx.Response(200, json=body)
        auth = r.headers["authorization"].removeprefix("Bearer ")
        self.calls.append(("ire", auth))
        if auth in self.reject:
            return httpx.Response(401, text=f"bad token {auth}")
        payload = json.loads(r.content)
        items = [{"operation": "INSERT", "sysId": "a" * 32} for _ in payload["items"]]
        return httpx.Response(200, json={"result": {"items": items}})

    def client(self, clock):
        return sn.ServiceNowClient(
            _connection(),
            transport=httpx.MockTransport(self.handler),
            resolver=_public,
            sleep=_no_sleep,
            clock=clock,
        )


PAYLOAD = {"items": [{"className": "x", "values": {}}], "relations": []}


async def test_token_is_refreshed_before_expires_in():
    fakes, clock = TokenFakes(expires_in=1800), Clock()
    async with fakes.client(clock) as client:
        await client.identify_reconcile(PAYLOAD)
        clock.t += 1700  # inside the lifetime less the margin: reused
        await client.identify_reconcile(PAYLOAD)
        clock.t += 60  # 1760s: within 60s of expiry, refreshed first
        await client.identify_reconcile(PAYLOAD)
    assert fakes.calls == [
        ("token", ""),
        ("ire", f"{BEARER}-1"),
        ("ire", f"{BEARER}-1"),
        ("token", ""),
        ("ire", f"{BEARER}-2"),
    ]


async def test_token_without_expires_in_is_kept_until_a_401():
    fakes, clock = TokenFakes(expires_in=None), Clock()
    async with fakes.client(clock) as client:
        await client.identify_reconcile(PAYLOAD)
        clock.t += 10**6
        fakes.reject.add(f"{BEARER}-1")
        await client.identify_reconcile(PAYLOAD)
    assert fakes.calls == [
        ("token", ""),
        ("ire", f"{BEARER}-1"),
        ("ire", f"{BEARER}-1"),
        ("token", ""),
        ("ire", f"{BEARER}-2"),
    ]


async def test_second_401_fails_without_looping_or_leaking(caplog):
    caplog.set_level(logging.DEBUG)
    fakes, clock = TokenFakes(), Clock()
    fakes.reject = {f"{BEARER}-1", f"{BEARER}-2", f"{BEARER}-3"}
    async with fakes.client(clock) as client:
        with pytest.raises(sn.ConnectorError) as exc:
            await client.identify_reconcile(PAYLOAD)
    assert str(exc.value) == "servicenow: identify and reconcile returned HTTP 401"
    # One refresh, one repeat; nothing more.
    assert [c[0] for c in fakes.calls] == ["token", "ire", "token", "ire"]
    assert BEARER not in caplog.text and CLIENT_SECRET not in caplog.text


async def test_basic_auth_401_is_not_retried():
    db = FakeSession()
    fakes = Fakes()
    fakes.fail_with = 401
    settings = _settings(
        oauth_client_id=None, oauth_client_secret=None, username="ps", password=PASSWORD
    )
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["error"] == "servicenow: identify and reconcile returned HTTP 401"
    assert len(fakes.sn_requests) == 1
