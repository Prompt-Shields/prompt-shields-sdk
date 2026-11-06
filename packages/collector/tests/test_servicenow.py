"""ServiceNow connector. Partner API and ServiceNow (OAuth token endpoint
and the IRE API) are mocked with httpx.MockTransport; no network, no DB."""

import base64
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

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
    "last_seen",
    [12345, 1.5, ["2026"], {"a": 1}, True, "nope", "", "0001-01-01T00:00:00+05:00"],
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


async def test_out_of_range_changed_at_is_a_failed_run_not_a_500():
    db = FakeSession({"connectors": {"servicenow": {"cursor": "2026-10-15T00:00:00Z"}}})
    fakes = Fakes(changes=[{"asset": _asset(), "changed_at": "0001-01-01T00:01:00Z"}])
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "error", out
    assert out["error"] == "invalid response data"


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
        # asset id -> risk mappings served by /assets/{id}/risks.
        self.risks: dict[str, list[dict]] = {}
        self.risks_status = 200
        # (asset sys_id, reference field) -> referenced record sys_id, and
        # (asset sys_id, key) -> value of cmdb_key_value related entries.
        self.references: dict[tuple[str, str], str] = {}
        self.key_values: dict[tuple[str, str], str] = {}
        self.flows: dict[str, list[dict]] = {}
        # Read-back (Table API): requests seen, CIs (by correlation_id) and
        # relations (by type) the instance pretends not to have.
        self.table_requests: list[httpx.Request] = []
        self.hidden_cis: set[str] = set()
        self.hidden_rel_types: set[str] = set()
        # IRE answering the items in reverse order, and IRE storing (and
        # reporting) a CI of the sent class under another class.
        self.reverse_ire = False
        self.reclassify: dict[str, str] = {}

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
        if r.url.path.endswith("/data-flows"):
            aid = r.url.path.split("/")[-2]
            return httpx.Response(
                200, json={"data": self.flows.get(aid, []), "meta": tenant}
            )
        if r.url.path.endswith("/risks"):
            if self.risks_status != 200:
                return httpx.Response(self.risks_status)
            aid = r.url.path.split("/")[-2]
            data = self.risks.get(aid, [])
            return httpx.Response(200, json={"data": data, "meta": tenant})
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
        if r.url.path.startswith(sn.TABLE_PATH + "/"):
            return self._table(r)
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
            # Vendors and assets: identification rule on correlation_id;
            # business unit records (cmn_department): on name.
            ident = item["values"].get("correlation_id") or item["values"]["name"]
            cls = self.reclassify.get(item["className"], item["className"])
            key = (cls, ident)
            sys_id = hashlib.md5("|".join(key).encode()).hexdigest()
            if key not in self.cmdb:
                op = "INSERT"
            elif self.cmdb[key] != item["values"]:
                op = "UPDATE"
            else:
                op = "NO_CHANGE"
            self.cmdb[key] = item["values"]
            out.append({"className": cls, "operation": op, "sysId": sys_id})
            sys_ids.append(sys_id)
        for rel in payload["relations"]:
            self.rels.add((sys_ids[rel["parent"]], rel["type"], sys_ids[rel["child"]]))
        by_internal = {
            item["internal_id"]: sys_id
            for item, sys_id in zip(payload["items"], sys_ids, strict=True)
            if "internal_id" in item
        }
        for ref in payload.get("referenceItems", []):
            key = (by_internal[ref["referencedBy"]], ref["referenceField"])
            self.references[key] = by_internal[ref["referenced"]]
        for item, sys_id in zip(payload["items"], sys_ids, strict=True):
            for rel in item.get("related", []):
                assert rel["className"] == "cmdb_key_value"
                self.key_values[(sys_id, rel["values"]["key"])] = rel["values"]["value"]
        if self.reverse_ire:
            out.reverse()
        return {"items": out, "relations": []}

    def _table(self, r: httpx.Request) -> httpx.Response:
        """Table API GET over the fake CMDB, honouring the query shapes the
        read-back sends (sys_idIN..., parentIN...^childIN...) and paging."""
        self.table_requests.append(r)
        assert r.method == "GET"
        table = r.url.path.rsplit("/", 1)[-1]
        p = r.url.params
        assert p["sysparm_exclude_reference_link"] == "true"
        query = p["sysparm_query"]
        if table == "cmdb_ci":
            assert query.startswith("sys_idIN") and "^" not in query
            assert p["sysparm_fields"] == "sys_id,sys_class_name,correlation_id"
            wanted = set(query.removeprefix("sys_idIN").split(","))
            rows = []
            for (cls, ident), values in self.cmdb.items():
                ref = values.get("correlation_id")
                sys_id = hashlib.md5(f"{cls}|{ident}".encode()).hexdigest()
                if ref and sys_id in wanted and ref not in self.hidden_cis:
                    rows.append(
                        {"sys_id": sys_id, "sys_class_name": cls, "correlation_id": ref}
                    )
        elif table == "cmdb_rel_ci":
            assert p["sysparm_fields"] == "parent,child,type.name"
            parents, children = query.split("^")
            parents = set(parents.removeprefix("parentIN").split(","))
            children = set(children.removeprefix("childIN").split(","))
            rows = [
                {"parent": pa, "child": ch, "type.name": t}
                for pa, t, ch in sorted(self.rels)
                if pa in parents and ch in children and t not in self.hidden_rel_types
            ]
        else:
            return httpx.Response(403)
        offset, limit = int(p["sysparm_offset"]), int(p["sysparm_limit"])
        return httpx.Response(200, json={"result": rows[offset : offset + limit]})

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


# ──────────────────────────────────────────────────────────────────────
# Data flows (opt-in: data_system_class)
# ──────────────────────────────────────────────────────────────────────

FLOW_REL = "Sends data to::Receives data from"


def _flow(src, dst, direction, fid=None, pii=None) -> dict:
    return {
        "id": fid or str(uuid.uuid4()),
        "source_system": src,
        "destination_system": dst,
        "direction": direction,
        "data_classification": "confidential",
        "detected_pii_types": pii,
    }


@pytest.mark.parametrize(
    "flow,edge",
    [
        (_flow("crm", "openai/gpt-4o", "input"), ("crm", True, False)),
        (_flow("crm", "openai/gpt-4o", "Inbound"), ("crm", True, False)),
        (_flow("openai/gpt-4o", "tickets", "output"), ("tickets", False, True)),
        (_flow("openai/gpt-4o", "tickets", "outbound"), ("tickets", False, True)),
        (_flow("  hr   db ", "x", "bi-directional"), ("hr db", True, True)),
        (_flow("unknown", "openai/gpt-4o", "input"), None),
        (_flow(" ", "openai/gpt-4o", "input"), None),
        (_flow(["crm"], "openai/gpt-4o", "input"), None),
        (_flow("crm", "openai/gpt-4o", "sideways"), None),
        ({"direction": None}, None),
    ],
)
def test_flow_edge(flow, edge):
    assert sn.flow_edge(flow) == edge


def test_system_ref_is_bounded_stable_and_case_insensitive():
    ref = sn.system_ref("Salesforce CRM")
    assert ref.startswith("promptshields:system:") and len(ref) <= 100
    assert sn.system_ref("  salesforce   crm ") == ref
    assert len(sn.system_ref("x" * 200)) == len(ref)
    assert sn.system_ref("other") != ref


def test_invalid_data_system_class_is_refused():
    with pytest.raises(ValueError):
        _connection(data_system_class="cmdb_ci; drop")


def test_data_flows_are_ignored_unless_data_system_class_is_set():
    flows = {A1: [_flow("crm", "openai/gpt-4o", "input")]}
    payload = sn.build_payload([_asset(A1)], _connection(), flows)
    assert [i["className"] for i in payload["items"]] == [
        "cmdb_ci_function_ai",
        "cmdb_ci_service",
    ]
    assert all(r["type"] != FLOW_REL for r in payload["relations"])


def test_payload_maps_data_flow_systems_and_relations():
    conn = _connection(data_system_class="cmdb_ci_appl")
    flows = {
        A1: [
            _flow("CRM", "openai/gpt-4o", "input", pii=["email"]),
            _flow("crm", "openai/gpt-4o", "input", pii=["phone", "email"]),
            _flow("openai/gpt-4o", "Tickets", "output"),
            _flow("unknown", "openai/gpt-4o", "input"),
        ],
        A2: [_flow("crm", "openai/gpt-4o", "input")],
    }
    payload = sn.build_payload([_asset(A1), _asset(A2)], conn, flows)
    items = payload["items"]
    # A1, openai, CRM, Tickets, A2 (openai and CRM shared within the payload).
    assert [i["className"] for i in items] == [
        "cmdb_ci_function_ai",
        "cmdb_ci_service",
        "cmdb_ci_appl",
        "cmdb_ci_appl",
        "cmdb_ci_function_ai",
    ]
    crm = items[2]
    assert crm["values"]["name"] == "CRM"
    assert crm["values"]["correlation_id"] == sn.system_ref("crm")
    assert crm["sys_object_source_info"]["source_native_key"] == sn.system_ref("crm")
    assert items[3]["values"]["name"] == "Tickets"
    desc = items[0]["values"]["short_description"]
    assert "Data flows: 4." in desc and "Detected PII types: email, phone." in desc
    assert payload["relations"] == [
        {"type": "Depends on::Used by", "parent": 0, "child": 1},
        {"type": FLOW_REL, "parent": 2, "child": 0},  # CRM sends to A1
        {"type": FLOW_REL, "parent": 0, "child": 3},  # A1 sends to Tickets
        {"type": "Depends on::Used by", "parent": 4, "child": 1},
        {"type": FLOW_REL, "parent": 2, "child": 4},  # CRM sends to A2
    ]


def test_systems_per_asset_are_capped_and_counted(monkeypatch):
    monkeypatch.setattr(sn, "MAX_SYSTEMS_PER_ASSET", 2)
    conn = _connection(data_system_class="cmdb_ci_appl", provider_class=None)
    flows = {A1: [_flow(f"s{i}", "m", "input") for i in range(4)]}
    [batch] = sn.build_payloads([_asset(A1)], conn, flows)
    assert len(batch.items) == 3 and len(batch.relations) == 2
    assert batch.flows_skipped == 2


def test_payloads_close_early_at_the_item_cap(monkeypatch):
    monkeypatch.setattr(sn, "MAX_ITEMS_PER_REQUEST", 7)
    conn = _connection(data_system_class="cmdb_ci_appl")
    flows = {
        A1: [_flow(f"a{i}", "m", "input") for i in range(3)],
        A2: [_flow(f"b{i}", "m", "input") for i in range(3)],
        A3: [],
    }
    batches = sn.build_payloads([_asset(A1), _asset(A2), _asset(A3)], conn, flows)
    # A1 is 5 items (asset, vendor, 3 systems); A2 would make 10 -> new
    # request, which A3 (at most 2 more) still fits into.
    assert [b.assets for b in batches] == [1, 2]
    for b in batches:
        p = b.payload()
        n = len(p["items"])
        assert all(0 <= r["parent"] < n and 0 <= r["child"] < n for r in p["relations"])


async def test_sync_pushes_data_flows_idempotently(caplog):
    caplog.set_level(logging.DEBUG)
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2)])
    fakes.flows = {
        A1: [
            _flow("crm", "openai/gpt-4o", "input", pii=["email"]),
            _flow("openai/gpt-4o", "tickets", "output"),
        ],
        A2: [_flow("crm", "openai/gpt-4o", "input"), _flow("x", "y", "?")],
    }
    settings = _settings(data_system_class="cmdb_ci_appl")
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    paths = [r.url.path for r in fakes.partner_requests]
    assert paths == [
        "/partner/v1/export",
        f"/partner/v1/assets/{A1}/data-flows",
        f"/partner/v1/assets/{A2}/data-flows",
    ]
    assert out["data_flows"] == 4 and out["data_flows_skipped"] == 1
    # 2 assets, openai, crm, tickets.
    assert out["ci_items"] == 5 and out["operations"] == {"INSERT": 5}
    flow_rels = {r for r in fakes.rels if r[1] == FLOW_REL}
    assert len(flow_rels) == 3 and len(fakes.rels) == 5
    systems = {k for k in fakes.cmdb if k[0] == "cmdb_ci_appl"}
    assert systems == {
        ("cmdb_ci_appl", sn.system_ref("crm")),
        ("cmdb_ci_appl", sn.system_ref("tickets")),
    }

    out = await sn.sync_servicenow(db, TENANT, full=True, **fakes.kwargs(settings))
    assert out["status"] == "ok" and out["operations"] == {"NO_CHANGE": 5}
    assert len(fakes.cmdb) == 5 and len(fakes.rels) == 5
    blob = json.dumps(db.tenant.settings) + json.dumps(out) + caplog.text
    for secret in SECRETS:
        assert secret not in blob


async def test_data_flows_are_not_read_by_default():
    db = FakeSession()
    fakes = Fakes()
    fakes.flows = {A1: [_flow("crm", "openai/gpt-4o", "input")]}
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok" and "data_flows" not in out
    assert [r.url.path for r in fakes.partner_requests] == ["/partner/v1/export"]


async def test_data_flow_read_failure_keeps_cursor_and_writes_nothing():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(changes=[{"asset": _asset(A1), "changed_at": cursor}])
    original = fakes.partner

    def partner(r):
        if r.url.path.endswith("/data-flows"):
            fakes.partner_requests.append(r)
            return httpx.Response(403)
        return original(r)

    fakes.partner = partner
    out = await sn.sync_servicenow(
        db, TENANT, **fakes.kwargs(_settings(data_system_class="cmdb_ci_appl"))
    )
    assert out["status"] == "error" and "data flows returned HTTP 403" in out["error"]
    assert fakes.sn_requests == []
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == cursor


# ──────────────────────────────────────────────────────────────────────
# Business units and risk mappings (opt-in)
# ──────────────────────────────────────────────────────────────────────

BU_OPTS = {"business_unit_table": "cmn_department"}
RISK = {
    "risk_category": "high_risk_annex_iii",
    "risk_level": "high",
    "framework": "EU_AI_ACT",
    "status": "confirmed",
}


def test_business_units_and_risks_are_off_by_default():
    conn = _connection()
    assert conn.business_unit_table is None and conn.push_risk_mappings is False
    payload = sn.build_payload([_asset(A1), _asset(A2)], conn)
    before = json.dumps(payload, sort_keys=True)
    counts = sn.add_business_units_and_risks(
        payload, sn.assets_by_ref([_asset(A1)]), {A1: [RISK]}, conn
    )
    assert counts == {"business_units": 0, "risk_entries": 0}
    assert json.dumps(payload, sort_keys=True) == before
    assert "referenceItems" not in payload


async def test_default_sync_reads_no_risks_and_sends_no_extras():
    db = FakeSession()
    fakes = Fakes()
    fakes.risks = {A1: [RISK]}
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs())
    assert out["status"] == "ok"
    assert "business_units" not in out and "risk_entries" not in out
    assert [r.url.path for r in fakes.partner_requests] == ["/partner/v1/export"]
    payload = fakes.payloads[0]
    assert set(payload) == {"items", "relations"}
    for item in payload["items"]:
        assert set(item) == {"className", "values", "sys_object_source_info"}


@pytest.mark.parametrize(
    "field,value",
    [("business_unit_table", "cmn_department; x"), ("business_unit_field", "a.b")],
)
def test_invalid_business_unit_config(field, value):
    with pytest.raises(ValueError):
        _connection(**{field: value})


def test_business_units_are_referenced_from_asset_cis():
    conn = _connection(**BU_OPTS)
    assets = [
        _asset(A1),
        _asset(A2, vendor="anthropic", business_unit="  Customer   Care "),
        _asset(A3, business_unit=None),
    ]
    payload = sn.build_payload(assets, conn)
    n_before = len(payload["items"])
    counts = sn.add_business_units_and_risks(
        payload, sn.assets_by_ref(assets), {}, conn
    )
    assert counts == {"business_units": 1, "risk_entries": 0}
    # One record for the business unit both assets share (whitespace
    # normalised), appended after the CIs so relation indexes still hold.
    bu = payload["items"][n_before:]
    ref = "promptshields:business-unit:Customer Care"
    assert bu == [
        {
            "className": "cmn_department",
            "internal_id": ref,
            "values": {"name": "Customer Care"},
            "sys_object_source_info": {
                "source_name": "PromptShields",
                "source_native_key": ref,
            },
        }
    ]
    assert payload["referenceItems"] == [
        {
            "referencedBy": f"promptshields:asset:{A1}",
            "referenced": ref,
            "referenceField": "department",
        },
        {
            "referencedBy": f"promptshields:asset:{A2}",
            "referenced": ref,
            "referenceField": "department",
        },
    ]
    by_ref = {i["values"].get("correlation_id"): i for i in payload["items"]}
    assert by_ref[f"promptshields:asset:{A1}"]["internal_id"] == (
        f"promptshields:asset:{A1}"
    )
    # No business unit: no reference, no internal id; vendors untouched.
    assert "internal_id" not in by_ref[f"promptshields:asset:{A3}"]
    assert "internal_id" not in by_ref["promptshields:provider:openai"]
    for rel in payload["relations"]:
        assert payload["items"][rel["child"]]["className"] == "cmdb_ci_service"


def test_business_unit_field_is_configurable():
    conn = _connection(business_unit_table="u_business_unit", business_unit_field="u_bu")
    payload = sn.build_payload([_asset(A1)], conn)
    sn.add_business_units_and_risks(payload, sn.assets_by_ref([_asset(A1)]), {}, conn)
    assert payload["items"][-1]["className"] == "u_business_unit"
    assert payload["referenceItems"][0]["referenceField"] == "u_bu"


def test_risk_entries_keep_the_highest_confirmed_level():
    risks = [
        RISK,
        {**RISK, "risk_level": "critical"},
        {**RISK, "risk_level": "low"},
        {"risk_category": "privacy", "risk_level": "MEDIUM", "framework": None},
        {**RISK, "risk_category": "suggested_only", "status": "suggested"},
        {**RISK, "risk_category": "rejected_one", "status": "rejected"},
        {**RISK, "risk_category": "bad_level", "risk_level": "extreme"},
        {**RISK, "risk_category": ""},
        {**RISK, "risk_category": None},
    ]
    assert sn.risk_entries(risks) == [
        {
            "className": "cmdb_key_value",
            "values": {
                "key": "promptshields:risk:EU_AI_ACT:high_risk_annex_iii",
                "value": "critical",
            },
        },
        {
            "className": "cmdb_key_value",
            "values": {"key": "promptshields:risk:custom:privacy", "value": "medium"},
        },
    ]


def test_long_risk_keys_are_hashed_not_truncated():
    a = sn.risk_key("custom", "x" * 200 + "a")
    b = sn.risk_key("custom", "x" * 200 + "b")
    assert a != b and len(a) <= sn.MAX_KEY and a.startswith("promptshields:risk:")


async def test_sync_pushes_business_units_and_risks():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, business_unit="Finance")])
    fakes.risks = {A1: [RISK, {**RISK, "status": "suggested", "risk_level": "low"}]}
    settings = _settings(push_risk_mappings=True, **BU_OPTS)
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    assert out["business_units"] == 2 and out["risk_entries"] == 1
    # Two assets, one vendor and two departments.
    assert out["ci_items"] == 5 and out["operations"] == {"INSERT": 5}
    paths = [r.url.path for r in fakes.partner_requests]
    assert paths == [
        "/partner/v1/export",
        f"/partner/v1/assets/{A1}/risks",
        f"/partner/v1/assets/{A2}/risks",
    ]

    def sys_id(cls, ident):
        return hashlib.md5(f"{cls}|{ident}".encode()).hexdigest()

    a1 = sys_id("cmdb_ci_function_ai", f"promptshields:asset:{A1}")
    a2 = sys_id("cmdb_ci_function_ai", f"promptshields:asset:{A2}")
    assert fakes.references == {
        (a1, "department"): sys_id("cmn_department", "Customer Care"),
        (a2, "department"): sys_id("cmn_department", "Finance"),
    }
    assert fakes.key_values == {
        (a1, "promptshields:risk:EU_AI_ACT:high_risk_annex_iii"): "high"
    }

    # Re-running updates the same records: nothing new is created.
    out = await sn.sync_servicenow(db, TENANT, full=True, **fakes.kwargs(settings))
    assert out["operations"] == {"NO_CHANGE": 5} and len(fakes.cmdb) == 5
    blob = json.dumps(db.tenant.settings) + json.dumps(out)
    for secret in SECRETS:
        assert secret not in blob


async def test_business_units_are_shared_once_per_request_across_batches():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2), _asset(A3)])
    settings = _settings(batch_size=2, **BU_OPTS)
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok" and out["requests"] == 2
    for p in fakes.payloads:
        depts = [i for i in p["items"] if i["className"] == "cmn_department"]
        assert len(depts) == 1
        internal = {i.get("internal_id") for i in p["items"]}
        for ref in p["referenceItems"]:
            assert ref["referenced"] in internal and ref["referencedBy"] in internal
    # One department record in the CMDB; every asset points at it.
    assert sum(k[0] == "cmn_department" for k in fakes.cmdb) == 1
    assert len(set(fakes.references.values())) == 1 and len(fakes.references) == 3


async def test_risk_read_without_scope_fails_and_keeps_cursor():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.risks_status = 403  # partner key lacks risks:read
    out = await sn.sync_servicenow(
        db, TENANT, **fakes.kwargs(_settings(push_risk_mappings=True))
    )
    assert out["status"] == "error"
    assert out["error"] == "partner-api: risk mappings returned HTTP 403"
    assert fakes.sn_requests == []
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == cursor


async def test_risk_read_of_another_tenant_fails_closed():
    db = FakeSession()
    fakes = Fakes()
    real = fakes.partner

    def partner(r):
        if r.url.path.endswith("/risks"):
            fakes.partner_requests.append(r)
            meta = {"tenant_id": str(uuid.uuid4())}
            return httpx.Response(200, json={"data": [RISK], "meta": meta})
        return real(r)

    fakes.partner = partner
    out = await sn.sync_servicenow(
        db, TENANT, **fakes.kwargs(_settings(push_risk_mappings=True))
    )
    assert out["status"] == "error"
    assert "does not belong to the configured tenant" in out["error"]
    assert fakes.sn_requests == []


async def test_business_units_risks_and_data_flows_together():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, business_unit="Finance")])
    fakes.flows = {
        A1: [{"direction": "input", "source_system": "crm"}],
        A2: [{"direction": "output", "destination_system": "warehouse"}],
    }
    fakes.risks = {A2: [RISK]}
    settings = _settings(
        data_system_class="cmdb_ci_appl", push_risk_mappings=True, **BU_OPTS
    )
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    assert out["business_units"] == 2 and out["risk_entries"] == 1
    (payload,) = fakes.payloads
    classes = [i["className"] for i in payload["items"]]
    assert classes.count("cmn_department") == 2 and classes.count("cmdb_ci_appl") == 2
    # Business unit records come after every CI the relations point at.
    first_bu = classes.index("cmn_department")
    for rel in payload["relations"]:
        assert rel["parent"] < first_bu and rel["child"] < first_bu
    assert len(fakes.rels) == 4 and len(fakes.references) == 2


# ──────────────────────────────────────────────────────────────────────
# Dry run
# ──────────────────────────────────────────────────────────────────────


async def test_dry_run_sends_nothing_and_records_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    cursor = "2026-10-01T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    before = json.dumps(db.tenant.settings)
    fakes = Fakes(assets=[_asset(A1), _asset(A2, vendor="anthropic")])
    out = await sn.sync_servicenow(
        db, TENANT, full=True, dry_run=True, **fakes.kwargs()
    )
    assert out["status"] == "ok" and out["dry_run"] is True, out
    assert out["assets"] == 2 and out["ci_items"] == 4 and out["relations"] == 2
    assert out["requests"] == 1 and "operations" not in out
    assert out["item_classes"] == {"cmdb_ci_function_ai": 2, "cmdb_ci_service": 2}
    assert out["relation_types"] == {"Depends on::Used by": 2}
    # The cursor a real run would store, but nothing stored and nothing sent.
    assert out["cursor"] == "2026-10-15T11:55:00+00:00"
    assert fakes.sn_requests == [] and fakes.cmdb == {}
    assert json.dumps(db.tenant.settings) == before
    # The Partner API is still read (and audited there); the result carries
    # counts, not asset data.
    assert [r.url.path for r in fakes.partner_requests] == ["/partner/v1/export"]
    assert "Support bot" not in json.dumps(out)
    blob = json.dumps(out) + caplog.text
    for secret in SECRETS:
        assert secret not in blob


async def test_dry_run_counts_business_units_and_risk_entries():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, business_unit="Finance")])
    fakes.risks = {A2: [RISK]}
    settings = _settings(push_risk_mappings=True, **BU_OPTS)
    out = await sn.sync_servicenow(db, TENANT, dry_run=True, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    assert out["business_units"] == 2 and out["risk_entries"] == 1
    assert out["item_classes"]["cmn_department"] == 2
    assert out["item_classes"]["cmdb_key_value"] == 1
    assert fakes.sn_requests == []


async def test_dry_run_ignores_verify_and_failures_record_nothing():
    db = FakeSession()
    fakes = Fakes()
    out = await sn.sync_servicenow(
        db, TENANT, dry_run=True, verify=True, **fakes.kwargs()
    )
    assert out["status"] == "ok" and "verification" not in out
    assert fakes.sn_requests == []
    fakes.key_tenant = str(uuid.uuid4())
    out = await sn.sync_servicenow(db, TENANT, dry_run=True, **fakes.kwargs())
    assert out["status"] == "error" and out["dry_run"] is True
    assert db.tenant.settings == {}


# ──────────────────────────────────────────────────────────────────────
# Read-back verification (Table API)
# ──────────────────────────────────────────────────────────────────────


def _basic_client(transport) -> sn.ServiceNowClient:
    conn = _connection(
        username="u", password=PASSWORD, oauth_client_id=None, oauth_client_secret=None
    )
    return sn.ServiceNowClient(
        conn, transport=transport, resolver=_public, sleep=_no_sleep
    )


def test_verify_is_off_by_default():
    assert _connection().verify is False


async def test_verify_reads_cis_and_relations_back():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, vendor="anthropic")])
    fakes.flows = {A1: [{"direction": "input", "source_system": "crm"}]}
    settings = _settings(data_system_class="cmdb_ci_appl", **BU_OPTS)
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    # 2 assets, 2 vendors, 1 system; 2 vendor and 1 flow relation. The
    # business unit record is not a CI and is not read back.
    assert out["verification"] == {
        "checked": 8,
        "found": 8,
        "missing_count": 0,
        "missing": [],
        "reclassified_count": 0,
        "reclassified": [],
        "cis_checked": 5,
        "relations_checked": 3,
    }
    tables = [r.url.path.rsplit("/", 1)[-1] for r in fakes.table_requests]
    assert tables == ["cmdb_ci", "cmdb_rel_ci"]
    # Reads only.
    assert {r.method for r in fakes.table_requests} == {"GET"}
    state = db.tenant.settings["connectors"]["servicenow"]
    assert state["last_verification"]["found"] == 8 and state["cursor"]


async def test_verify_follows_the_tenant_setting():
    db = FakeSession()
    fakes = Fakes()
    out = await sn.sync_servicenow(db, TENANT, **fakes.kwargs(_settings(verify=True)))
    assert out["verification"]["missing_count"] == 0
    out = await sn.sync_servicenow(
        db, TENANT, full=True, verify=False, **fakes.kwargs(_settings(verify=True))
    )
    assert "verification" not in out
    out = await sn.sync_servicenow(db, TENANT, full=True, **fakes.kwargs())
    assert "verification" not in out and len(fakes.table_requests) == 2


@pytest.mark.parametrize(
    "hide,missing",
    [
        # A CI that is not there; its relation cannot be checked either.
        (
            {"cis": {f"promptshields:asset:{A1}"}},
            [
                f"promptshields:asset:{A1}",
                (
                    f"promptshields:asset:{A1} Depends on::Used by "
                    "promptshields:provider:openai"
                ),
            ],
        ),
        (
            {"rels": {"Depends on::Used by"}},
            [
                (
                    f"promptshields:asset:{A1} Depends on::Used by "
                    "promptshields:provider:openai"
                )
            ],
        ),
    ],
)
async def test_missing_ci_or_relation_fails_and_keeps_cursor(hide, missing):
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.hidden_cis = hide.get("cis", set())
    fakes.hidden_rel_types = hide.get("rels", set())
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["status"] == "error"
    assert out["error"] == (
        f"servicenow: {len(missing)} of 3 CIs and relations not found after sync"
    )
    assert out["verification"]["missing"] == missing
    state = db.tenant.settings["connectors"]["servicenow"]
    assert state["cursor"] == cursor and state["last_status"] == "error"
    assert state["last_verification"]["missing_count"] == len(missing)


def _expected_one(sent: str, ire: str) -> "sn.Expected":
    expected = sn.Expected()
    expected.cis = {"promptshields:asset:x": sent}
    expected.returned = {"a" * 32: ire}
    return expected


def _one_row(cls: str) -> httpx.MockTransport:
    row = {
        "sys_id": "a" * 32,
        "sys_class_name": cls,
        "correlation_id": "promptshields:asset:x",
    }
    return httpx.MockTransport(lambda r: httpx.Response(200, json={"result": [row]}))


@pytest.mark.parametrize(
    "ire,row,bad",
    [
        # The CMDB holds the CI under an unrelated class.
        ("cmdb_ci_function_ai", "cmdb_ci_appl", "cmdb_ci_appl"),
        # IRE reports an unrelated class for the same sys_id.
        ("cmdb_ci_appl", "cmdb_ci_function_ai", "cmdb_ci_appl"),
        # A parent class is not a subclass.
        ("cmdb_ci_function_ai", "cmdb_ci", "cmdb_ci"),
    ],
)
async def test_verify_rejects_a_ci_of_an_unrelated_class(ire, row, bad):
    async with _basic_client(_one_row(row)) as client:
        out = await client.verify(_expected_one("cmdb_ci_function_ai", ire))
    assert out["missing"] == [f"promptshields:asset:x (class {bad})"]
    assert out["missing_count"] == 1 and out["reclassified_count"] == 0


async def test_verify_checks_the_row_class_when_ire_names_none():
    async with _basic_client(_one_row("cmdb_ci_appl")) as client:
        out = await client.verify(_expected_one("cmdb_ci_appl", ""))
    assert out["missing_count"] == 0 and out["found"] == 1


async def test_verify_class_labels_from_the_response_are_not_echoed():
    async with _basic_client(_one_row("<script>x")) as client:
        out = await client.verify(_expected_one("cmdb_ci_appl", "cmdb_ci_appl"))
    assert out["missing"] == ["promptshields:asset:x (class ?)"]


@pytest.mark.parametrize(
    "sent,got",
    [
        ("cmdb_ci_service", "cmdb_ci_service_discovered"),
        ("cmdb_ci_service", "service_offering"),
        ("cmdb_ci_appl", "cmdb_ci_appl_ai_application"),
    ],
)
async def test_verify_accepts_a_known_subclass_as_reclassified(sent, got):
    async with _basic_client(_one_row(got)) as client:
        out = await client.verify(_expected_one(sent, got))
    assert out["missing_count"] == 0 and out["found"] == 1
    assert out["reclassified"] == [f"promptshields:asset:x ({sent} -> {got})"]


async def test_verify_rejects_two_cis_with_one_correlation_id():
    expected = _expected_one("cmdb_ci_appl", "cmdb_ci_appl")
    expected.returned["b" * 32] = "cmdb_ci_appl"
    rows = [
        {
            "sys_id": s,
            "sys_class_name": "cmdb_ci_appl",
            "correlation_id": "promptshields:asset:x",
        }
        for s in ("a" * 32, "b" * 32)
    ]
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"result": rows})
    )
    async with _basic_client(transport) as client:
        out = await client.verify(expected)
    assert out["missing"] == ["promptshields:asset:x (duplicate CIs)"]


async def test_verify_does_not_depend_on_the_order_of_ire_answers():
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2, vendor="anthropic")])
    fakes.flows = {A1: [{"direction": "input", "source_system": "crm"}]}
    fakes.reverse_ire = True
    settings = _settings(data_system_class="cmdb_ci_appl", **BU_OPTS)
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs(settings))
    assert out["status"] == "ok", out
    v = out["verification"]
    assert (v["found"], v["missing_count"], v["reclassified_count"]) == (8, 0, 0)


async def test_reclassified_ci_is_reported_not_failed():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.reclassify = {"cmdb_ci_service": "cmdb_ci_service_discovered"}
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["status"] == "ok", out
    v = out["verification"]
    assert v["missing_count"] == 0 and v["found"] == 3
    assert v["reclassified"] == [
        "promptshields:provider:openai (cmdb_ci_service -> cmdb_ci_service_discovered)"
    ]
    state = db.tenant.settings["connectors"]["servicenow"]
    assert state["cursor"] != cursor
    assert state["last_verification"]["reclassified_count"] == 1


async def test_unrelated_class_mismatch_fails_and_keeps_cursor():
    cursor = "2026-10-15T00:00:00+00:00"
    db = FakeSession({"connectors": {"servicenow": {"cursor": cursor}}})
    fakes = Fakes(
        changes=[{"asset": _asset(), "changed_at": "2026-10-15T13:00:00+00:00"}]
    )
    fakes.reclassify = {"cmdb_ci_function_ai": "cmdb_ci_appl"}
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["status"] == "error"
    ref = f"promptshields:asset:{A1}"
    # The CI and, with it, its relation to the vendor are not found.
    assert out["verification"]["missing"] == [
        f"{ref} (class cmdb_ci_appl)",
        f"{ref} Depends on::Used by promptshields:provider:openai",
    ]
    assert db.tenant.settings["connectors"]["servicenow"]["cursor"] == cursor


async def test_verify_pages_and_chunks(monkeypatch):
    monkeypatch.setattr(sn, "VERIFY_PAGE", 2)
    monkeypatch.setattr(sn, "VERIFY_CHUNK", 4)
    db = FakeSession()
    fakes = Fakes(assets=[_asset(A1), _asset(A2), _asset(A3)])
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["status"] == "ok", out
    assert out["verification"]["found"] == 7  # 3 assets, 1 vendor, 3 relations
    params = [dict(r.url.params) for r in fakes.table_requests]
    ci = [p for p in params if p["sysparm_fields"].startswith("sys_id")]
    # 4 CIs: one chunk, read in pages of 2 (2, 2, then an empty page).
    assert [p["sysparm_offset"] for p in ci] == ["0", "2", "4"]
    rel = [p for p in params if p["sysparm_fields"].startswith("parent")]
    assert len({p["sysparm_query"] for p in rel}) == 2  # 3 relations, 2 per chunk


async def test_verify_gives_up_on_endless_pages(monkeypatch):
    monkeypatch.setattr(sn, "VERIFY_PAGE", 1)
    monkeypatch.setattr(sn, "VERIFY_MAX_PAGES", 3)
    body = {"result": [{"sys_id": "a" * 32}]}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    async with _basic_client(transport) as client:
        with pytest.raises(sn.ConnectorError, match="too many rows"):
            await client.query_table(
                "cmdb_ci", "sys_idIN" + "a" * 32, ["sys_id"], "CI lookup"
            )
        with pytest.raises(sn.ConnectorError, match="unexpected table"):
            await client.query_table("sys_user", "", ["sys_id"], "user lookup")


@pytest.mark.parametrize("body", [{"result": {"a": 1}}, ["x"], {"error": "x"}])
async def test_verify_unexpected_table_body_is_a_failed_run(body):
    db = FakeSession()
    fakes = Fakes()
    real = fakes._table

    def table(r):
        real(r)
        return httpx.Response(200, json=body)

    fakes._table = table
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["status"] == "error"
    assert out["error"] == "servicenow: CI lookup returned an unexpected body"


async def test_verify_table_403_does_not_leak():
    db = FakeSession()
    fakes = Fakes()

    def table(r):
        fakes.table_requests.append(r)
        return httpx.Response(403, text=f"no read on cmdb_ci {CLIENT_SECRET}")

    fakes._table = table
    out = await sn.sync_servicenow(db, TENANT, verify=True, **fakes.kwargs())
    assert out["error"] == "servicenow: CI lookup returned HTTP 403"
    assert CLIENT_SECRET not in json.dumps(db.tenant.settings) + json.dumps(out)


# Responses recorded in the documented IRE and Table API shapes (see
# fixtures/servicenow): IRE answers each item with its sys_id; with
# sysparm_exclude_reference_link the Table API returns reference fields
# as plain sys_ids and a dot-walked field under its dotted name.
FIXTURES = Path(__file__).parent / "fixtures" / "servicenow"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def test_recorded_responses_round_trip():
    db = FakeSession()
    fakes = Fakes()
    seen: list[str] = []

    def servicenow(r: httpx.Request) -> httpx.Response:
        seen.append(f"{r.method} {r.url.path}")
        if r.url.path == sn.TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": BEARER, "expires_in": 1800}
            )
        assert r.headers["authorization"] == f"Bearer {BEARER}"
        if r.url.path == sn.IRE_PATH:
            return httpx.Response(201, json=_fixture("identifyreconcile.json"))
        table = r.url.path.rsplit("/", 1)[-1]
        if table == "cmdb_ci":
            query = r.url.params["sysparm_query"].removeprefix("sys_idIN")
            assert set(query.split(",")) == {
                "3f1c2b7a9d5e4f60a1b2c3d4e5f60718",
                "b7e9d1c3a5f74e2d9c0b1a2f3e4d5c6b",
            }
        return httpx.Response(200, json=_fixture(f"table_{table}.json"))

    kw = fakes.kwargs()
    kw["servicenow_transport"] = httpx.MockTransport(servicenow)
    out = await sn.sync_servicenow(db, TENANT, verify=True, **kw)
    assert out["status"] == "ok", out
    assert out["operations"] == {"INSERT": 1, "NO_CHANGE": 1}
    assert out["verification"]["found"] == 3
    assert seen == [
        f"POST {sn.TOKEN_PATH}",
        f"POST {sn.IRE_PATH}",
        "GET /api/now/table/cmdb_ci",
        "GET /api/now/table/cmdb_rel_ci",
    ]


def _recorded(ire: dict, cis: dict) -> httpx.MockTransport:
    def servicenow(r: httpx.Request) -> httpx.Response:
        if r.url.path == sn.TOKEN_PATH:
            return httpx.Response(
                200, json={"access_token": BEARER, "expires_in": 1800}
            )
        if r.url.path == sn.IRE_PATH:
            return httpx.Response(201, json=ire)
        table = r.url.path.rsplit("/", 1)[-1]
        return httpx.Response(
            200, json=cis if table == "cmdb_ci" else _fixture(f"table_{table}.json")
        )

    return httpx.MockTransport(servicenow)


async def test_recorded_responses_reordered_ire_answer_passes():
    ire = _fixture("identifyreconcile.json")
    ire["result"]["items"].reverse()
    kw = Fakes().kwargs()
    kw["servicenow_transport"] = _recorded(ire, _fixture("table_cmdb_ci.json"))
    out = await sn.sync_servicenow(FakeSession(), TENANT, verify=True, **kw)
    assert out["status"] == "ok", out
    assert out["verification"]["found"] == 3


async def test_recorded_responses_reordered_ire_answer_cannot_pass_falsely():
    # Two answers, one sys_id: the vendor CI was not written, whatever
    # order IRE answered in.
    ire = _fixture("identifyreconcile.json")
    items = ire["result"]["items"]
    items[0]["sysId"] = items[1]["sysId"]
    items[0]["className"] = items[1]["className"]
    items.reverse()
    cis = _fixture("table_cmdb_ci.json")
    cis["result"] = [r for r in cis["result"] if r["sys_id"] == items[0]["sysId"]]
    kw = Fakes().kwargs()
    kw["servicenow_transport"] = _recorded(ire, cis)
    out = await sn.sync_servicenow(FakeSession(), TENANT, verify=True, **kw)
    assert out["status"] == "error"
    assert out["verification"]["missing"][0] == f"promptshields:asset:{A1}"


async def test_recorded_responses_reclassified_vendor():
    ire = _fixture("identifyreconcile.json")
    ire["result"]["items"][1]["className"] = "cmdb_ci_service_auto"
    cis = _fixture("table_cmdb_ci.json")
    cis["result"][1]["sys_class_name"] = "cmdb_ci_service_auto"
    kw = Fakes().kwargs()
    kw["servicenow_transport"] = _recorded(ire, cis)
    out = await sn.sync_servicenow(FakeSession(), TENANT, verify=True, **kw)
    assert out["status"] == "ok", out
    assert out["verification"]["reclassified"] == [
        "promptshields:provider:openai (cmdb_ci_service -> cmdb_ci_service_auto)"
    ]


async def test_no_verify_skips_the_read_back_the_tenant_enables():
    db = FakeSession()
    fakes = Fakes()
    out = await sn.sync_servicenow(
        db, TENANT, verify=False, **fakes.kwargs(_settings(verify=True))
    )
    assert out["status"] == "ok" and "verification" not in out
    assert fakes.table_requests == []
    assert "last_verification" not in db.tenant.settings["connectors"]["servicenow"]


def test_cli_parses_dry_run_and_verify(monkeypatch, capsys):
    calls = []

    async def fake_main(tenant, full, dry_run=False, verify=None):
        calls.append((tenant, full, dry_run, verify))
        return {}

    monkeypatch.setattr(sn, "_main", fake_main)
    sn.main(["--tenant", str(TENANT)])
    sn.main(["--tenant", str(TENANT), "--dry-run", "--verify", "--full"])
    sn.main(["--tenant", str(TENANT), "--no-verify"])
    assert calls == [
        (TENANT, False, False, None),
        (TENANT, True, True, True),
        (TENANT, False, False, False),
    ]
    with pytest.raises(SystemExit):
        sn.main(["--tenant", str(TENANT), "--verify", "--no-verify"])
