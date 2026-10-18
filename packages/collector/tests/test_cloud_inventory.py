"""Cloud AI inventory (platform signal), Azure. HTTP is mocked; no network,
no credentials, no DB."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector import cloud_inventory as ci
from db.models import AIAsset

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
SECRET = "azure-client-SECRET-value"
TOKEN = "eyJ-arm-access-token"
SUB = "aaaaaaaa-0000-0000-0000-000000000001"
ACCOUNT = (
    f"/subscriptions/{SUB}/resourceGroups/rg-ai/providers/"
    "Microsoft.CognitiveServices/accounts/aoai-se"
)
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


async def _no_sleep(_s: float) -> None:
    return None


class FakeSession:
    """Just enough of AsyncSession for store_inventory."""

    def __init__(self):
        self.rows: dict[uuid.UUID, AIAsset] = {}
        self.commits = 0

    async def get(self, model, ident):
        return self.rows.get(ident)

    def add(self, obj):
        self.rows[obj.id] = obj

    async def execute(self, _stmt):
        # Row locks and the webhook outbox's subscription lookup: no rows.
        return _NoRows()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1


class _NoRows:
    def scalars(self):
        return iter(())

    def all(self):
        return []


def _creds(**kw) -> ci.AzureInventoryCredentials:
    return ci.AzureInventoryCredentials(
        directory_id="contoso.onmicrosoft.com",
        client_id="app-id",
        client_secret=SECRET,
        **kw,
    )


def _settings(**kw) -> ci.CloudInventorySettings:
    return ci.CloudInventorySettings(
        cloud_inventory={str(TENANT): ci.TenantCloudCredentials(azure=_creds(**kw))}
    )


def _arm_handler(calls: list[httpx.Request], *, throttle_first_list: bool = False):
    state = {"throttled": not throttle_first_list}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host, path = request.url.host, request.url.path
        if host == "login.microsoftonline.com":
            assert path == "/contoso.onmicrosoft.com/oauth2/v2.0/token"
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["client_secret"] == SECRET
            assert form["scope"] == "https://management.azure.com/.default"
            return httpx.Response(200, json={"access_token": TOKEN})

        assert host == "management.azure.com"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if path == "/subscriptions":
            return httpx.Response(
                200,
                json={"value": [{"subscriptionId": SUB}, {"subscriptionId": "bad"}]},
            )
        if path.endswith("/Microsoft.CognitiveServices/accounts"):
            if not state["throttled"]:
                state["throttled"] = True
                return httpx.Response(429, headers={"retry-after": "3"})
            if request.url.params.get("page") != "2":
                return httpx.Response(
                    200,
                    json={
                        "value": [
                            {
                                "id": ACCOUNT,
                                "kind": "OpenAI",
                                "location": "swedencentral",
                                "tags": {
                                    "Owner": "jane.doe@acme.com",
                                    "env": "prod",
                                    "Department": "HR",
                                },
                            },
                            {
                                "id": ACCOUNT.replace("aoai-se", "speech"),
                                "kind": "SpeechServices",
                                "location": "westeurope",
                            },
                        ],
                        "nextLink": (
                            f"https://management.azure.com/subscriptions/{SUB}/providers/"
                            "Microsoft.CognitiveServices/accounts?api-version=2024-10-01&page=2"
                        ),
                    },
                )
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "/subscriptions/../../evil",
                            "kind": "OpenAI",
                            "location": "eastus",
                        }
                    ]
                },
            )
        if path == f"{ACCOUNT}/deployments":
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "name": "chat-prod",
                            "properties": {
                                "model": {"format": "OpenAI", "name": "gpt-4o"},
                                "provisioningState": "Succeeded",
                            },
                        },
                        {
                            "name": "embed",
                            "tags": {"owner": "ml-team@acme.com", "env": "dev"},
                            "properties": {
                                "model": {
                                    "format": "OpenAI",
                                    "name": "text-embedding-3-small",
                                },
                                "provisioningState": "Succeeded",
                            },
                        },
                        {
                            "name": "broken",
                            "properties": {
                                "model": {"format": "OpenAI", "name": "gpt-4o"},
                                "provisioningState": "Failed",
                            },
                        },
                        {
                            "name": "paused",
                            "properties": {
                                "model": {"format": "OpenAI", "name": "gpt-4o"},
                                "provisioningState": "Disabled",
                            },
                        },
                    ]
                },
            )
        raise AssertionError(f"unexpected request {request.url}")

    return handler


async def test_azure_lists_deployments_with_region_and_tags():
    calls: list[httpx.Request] = []
    summary, deps = await ci.fetch_azure_deployments(
        _creds(),
        transport=httpx.MockTransport(_arm_handler(calls, throttle_first_list=True)),
        sleep=_no_sleep,
    )
    assert summary == {"accounts": 1}  # speech skipped, malformed id rejected
    # Failed and Disabled deployments are not serving and are skipped.
    assert [(d.resource_id, d.region, d.model) for d in deps] == [
        (f"{ACCOUNT}/deployments/chat-prod", "swedencentral", "gpt-4o"),
        (f"{ACCOUNT}/deployments/embed", "swedencentral", "text-embedding-3-small"),
    ]
    assert deps[1].tags["owner"] == "ml-team@acme.com"
    assert {c.url.host for c in calls} == {
        "login.microsoftonline.com",
        "management.azure.com",
    }
    # Every call is a read: one token POST, everything else GET.
    assert [c.method for c in calls].count("POST") == 1
    assert {c.method for c in calls if c.url.host == "management.azure.com"} == {"GET"}


async def test_azure_token_is_renewed_before_it_expires():
    now = [0.0]

    def clock() -> float:
        now[0] += 120.0
        return now[0]

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            state["n"] += 1
            state["token"] = f"tok-{state['n']}"
            # Some endpoints send expires_in as a string.
            return httpx.Response(
                200, json={"access_token": state["token"], "expires_in": "600"}
            )
        # Always the latest token.
        assert request.headers["authorization"] == f"Bearer {state['token']}"
        return httpx.Response(200, json={"value": []})

    subs = [f"aaaaaaaa-0000-0000-0000-00000000000{i}" for i in range(1, 7)]
    summary, deps = await ci.fetch_azure_deployments(
        _creds(subscription_ids=subs),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        clock=clock,
    )
    assert summary == {"accounts": 0} and deps == []
    assert state["n"] >= 3


async def test_configured_subscriptions_skip_enumeration():
    calls: list[httpx.Request] = []
    await ci.fetch_azure_deployments(
        _creds(subscription_ids=[SUB]),
        transport=httpx.MockTransport(_arm_handler(calls)),
        sleep=_no_sleep,
    )
    assert "/subscriptions" not in [c.url.path for c in calls]


async def test_next_link_to_another_host_is_refused():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        assert request.url.host == "management.azure.com"
        return httpx.Response(
            200, json={"value": [], "nextLink": "https://evil.example/steal"}
        )

    with pytest.raises(ci.CloudInventoryError, match="nextLink"):
        await ci.fetch_azure_deployments(
            _creds(), transport=httpx.MockTransport(handler), sleep=_no_sleep
        )


SUB_DENIED = "aaaaaaaa-0000-0000-0000-000000000002"


async def test_a_failing_subscription_does_not_stop_the_others():
    calls: list[httpx.Request] = []
    ok = _arm_handler(calls)

    def handler(request: httpx.Request) -> httpx.Response:
        if SUB_DENIED in request.url.path:
            return httpx.Response(403, json={"error": {"code": "AuthorizationFailed"}})
        return ok(request)

    summary, deps = await ci.fetch_azure_deployments(
        _creds(subscription_ids=[SUB_DENIED, SUB]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert summary == {
        "accounts": 1,
        "subscription_errors": {SUB_DENIED: "azure: account list returned HTTP 403"},
    }
    assert len(deps) == 2

    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        clouds=["azure"],
        settings=_settings(subscription_ids=[SUB_DENIED, SUB]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        now=NOW,
    )
    assert result["clouds"]["azure"]["status"] == "ok"
    assert result["clouds"]["azure"]["subscription_errors"] == summary[
        "subscription_errors"
    ]
    assert result["clouds"]["azure"]["deployments"] == 2


async def test_every_subscription_failing_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(403)

    with pytest.raises(ci.CloudInventoryError, match="HTTP 403"):
        await ci.fetch_azure_deployments(
            _creds(subscription_ids=[SUB, SUB_DENIED]),
            transport=httpx.MockTransport(handler),
            sleep=_no_sleep,
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"value": "not-a-list"}),
        httpx.Response(200, json={"value": [1, "x"]}),
        httpx.Response(200, json={"value": [], "nextLink": 7}),
    ],
)
async def test_malformed_arm_response_is_a_cloud_error_not_a_crash(response):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        return response

    with pytest.raises(ci.CloudInventoryError):
        await ci.fetch_azure_deployments(
            _creds(), transport=httpx.MockTransport(handler), sleep=_no_sleep
        )
    # Through the pull: reported per cloud, not raised (no HTTP 500).
    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        clouds=["azure"],
        settings=_settings(subscription_ids=[SUB]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert result["clouds"]["azure"]["status"] == "error"


async def test_malformed_deployment_fields_are_tolerated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        if request.url.path.endswith("/accounts"):
            return httpx.Response(
                200, json={"value": [{"id": ACCOUNT, "kind": "OpenAI", "location": 5}]}
            )
        return httpx.Response(
            200,
            json={
                "value": [
                    {"name": "a", "properties": "x"},
                    {"name": "b", "properties": {"model": ["gpt-4o"]}},
                ]
            },
        )

    _, deps = await ci.fetch_azure_deployments(
        _creds(subscription_ids=[SUB]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert [(d.region, d.model) for d in deps] == [(None, None), (None, None)]


async def test_unknown_clouds_are_rejected():
    with pytest.raises(ValueError, match="unknown clouds: gpc"):
        await ci.pull_cloud_inventory(
            FakeSession(), TENANT, clouds=["azure", "gpc"], settings=_settings()
        )


async def test_pull_stores_assets_and_is_idempotent():
    db = FakeSession()
    for i in range(2):
        result = await ci.pull_cloud_inventory(
            db,
            TENANT,
            settings=_settings(),
            transport=httpx.MockTransport(_arm_handler([])),
            sleep=_no_sleep,
            now=NOW + timedelta(days=i),
        )
    assert result["clouds"]["azure"] == {
        "status": "ok",
        "accounts": 1,
        "deployments": 2,
        "assets_created": 0,
        "assets_updated": 2,
    }
    assert db.commits == 2
    assets = sorted(db.rows.values(), key=lambda a: a.model)
    assert [
        (
            a.tenant_id,
            a.vendor,
            a.model,
            a.region,
            a.owner_email,
            a.environment,
            a.business_unit,
            a.discovery_source,
            a.confidence,
        )
        for a in assets
    ] == [
        (
            TENANT,
            "microsoft",
            "gpt-4o",
            "swedencentral",
            "jane.doe@acme.com",
            "production",
            "HR",
            ["platform_signal"],
            "medium",
        ),
        (
            TENANT,
            "microsoft",
            "text-embedding-3-small",
            "swedencentral",
            "ml-team@acme.com",
            "dev",
            "HR",
            ["platform_signal"],
            "medium",
        ),
    ]
    assert assets[0].calling_service == f"azure:{ACCOUNT}/deployments/chat-prod"
    assert assets[0].first_seen == NOW
    assert assets[0].last_seen == NOW + timedelta(days=1)


async def test_rerun_keeps_curated_owner_and_reactivates():
    db = FakeSession()
    kw = {
        "settings": _settings(),
        "transport": httpx.MockTransport(_arm_handler([])),
        "sleep": _no_sleep,
    }
    await ci.pull_cloud_inventory(db, TENANT, now=NOW, **kw)
    asset = next(a for a in db.rows.values() if a.model == "gpt-4o")
    asset.owner_email = "curated@acme.com"
    asset.discovery_source = ["sdk"]
    asset.lifecycle_state = "stale"
    await ci.pull_cloud_inventory(db, TENANT, now=NOW + timedelta(days=40), **kw)
    assert asset.owner_email == "curated@acme.com"
    assert asset.discovery_source == ["sdk", "platform_signal"]
    assert asset.confidence == "verified"
    assert asset.lifecycle_state == "active"


def test_asset_ids_are_tenant_scoped_and_stable():
    d = ci.CloudDeployment("azure", f"{ACCOUNT}/deployments/x", "eastus", "m", None)
    other = uuid.uuid4()
    assert ci.asset_id_for(TENANT, d) == ci.asset_id_for(TENANT, d)
    assert ci.asset_id_for(TENANT, d) != ci.asset_id_for(other, d)


async def test_not_configured_tenant_makes_no_calls():
    def handler(request):
        raise AssertionError("no call expected")

    result = await ci.pull_cloud_inventory(
        FakeSession(),
        uuid.uuid4(),
        settings=_settings(),
        transport=httpx.MockTransport(handler),
    )
    assert result["clouds"] == {
        "azure": {"status": "not_configured"},
        "aws": {"status": "not_configured"},
        "gcp": {"status": "not_configured"},
    }


async def test_errors_and_logs_never_contain_the_secret(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client", "echo": SECRET})

    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert result["clouds"]["azure"]["status"] == "error"
    blob = json.dumps(result) + caplog.text + repr(_settings())
    assert SECRET not in blob


async def test_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(ci.CloudInventoryError, match="retries"):
        await ci.fetch_azure_deployments(
            _creds(), transport=httpx.MockTransport(handler), sleep=_no_sleep
        )


def test_credentials_come_from_env(monkeypatch):
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY",
        json.dumps(
            {
                str(TENANT): {
                    "azure": {
                        "directory_id": "contoso.onmicrosoft.com",
                        "client_id": "app",
                        "client_secret": SECRET,
                        "subscription_ids": [SUB],
                    }
                }
            }
        ),
    )
    creds = ci.credentials_for_tenant(TENANT)
    assert creds.azure.client_secret.get_secret_value() == SECRET
    assert ci.credentials_for_tenant(uuid.uuid4()).azure is None


def test_env_tenant_keys_match_case_insensitively(monkeypatch):
    tenant = uuid.UUID("abcdef01-2345-6789-abcd-ef0123456789")
    azure = {
        "directory_id": "contoso.onmicrosoft.com",
        "client_id": "app",
        "client_secret": SECRET,
    }
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY", json.dumps({str(tenant).upper(): {"azure": azure}})
    )
    assert ci.credentials_for_tenant(tenant).azure.client_id == "app"


@pytest.mark.parametrize(
    "keys",
    [
        ["not-a-tenant"],
        # The same tenant twice, differing only in case.
        ["ABCDEF01-2345-6789-ABCD-EF0123456789", "abcdef01-2345-6789-abcd-ef0123456789"],
    ],
)
def test_env_tenant_keys_must_be_distinct_uuids(monkeypatch, keys):
    azure = {"directory_id": "contoso.com", "client_id": "a", "client_secret": SECRET}
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY", json.dumps({k: {"azure": azure} for k in keys})
    )
    with pytest.raises(Exception) as exc_info:
        ci.credentials_for_tenant(TENANT)
    assert SECRET not in str(exc_info.value)


@pytest.mark.parametrize(
    "azure",
    [
        {"directory_id": "x/../evil", "client_id": "a", "client_secret": SECRET},
        {
            "directory_id": "contoso.com",
            "client_id": "a",
            "client_secret": SECRET,
            "subscription_ids": ["../x"],
        },
        SECRET,
    ],
)
def test_malformed_env_is_rejected_without_echoing_the_secret(monkeypatch, azure):
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY", json.dumps({str(TENANT): {"azure": azure}})
    )
    with pytest.raises(Exception) as exc_info:
        ci.credentials_for_tenant(TENANT)
    assert SECRET not in str(exc_info.value)


def test_tag_mapping():
    assert ci.owner_from_tags({"OWNER": " a@b.com "}) == "a@b.com"
    assert ci.owner_from_tags({"owner": "team-ai"}) is None
    assert ci.environment_from_tags({"Environment": "Staging"}) == "staging"
    assert ci.environment_from_tags({"env": "sandbox"}) is None


PULL_URL = "/api/v1/admin/cloud-inventory/pull"


def test_pull_endpoint_is_absent_without_admin_token(monkeypatch):
    from collector.app import app
    from fastapi.testclient import TestClient

    monkeypatch.delenv("PS_ADMIN_TOKEN", raising=False)
    resp = TestClient(app).post(
        PULL_URL,
        json={"tenant_id": str(TENANT)},
        headers={"Authorization": "Bearer ps-test-key-12345"},
    )
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "auth", [None, "Bearer ps-test-key-12345", "Bearer wrong-token", "op-token"]
)
def test_pull_endpoint_rejects_tenant_keys_and_bad_tokens(monkeypatch, auth):
    from collector.app import app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PS_ADMIN_TOKEN", "op-token")
    headers = {"Authorization": auth} if auth else {}
    resp = TestClient(app).post(
        PULL_URL, json={"tenant_id": str(TENANT)}, headers=headers
    )
    assert resp.status_code == 401


def test_request_body_cannot_carry_credentials():
    # Unknown fields such as a client secret are ignored, never used.
    body = ci.CloudInventoryPullBody.model_validate(
        {"tenant_id": str(TENANT), "azure": {"client_secret": "x"}}
    )
    assert not hasattr(body, "azure")
