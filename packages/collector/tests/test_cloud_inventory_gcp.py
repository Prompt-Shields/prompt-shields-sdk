"""Cloud AI inventory (platform signal), Google Cloud Vertex AI. HTTP is
mocked; no network, no real credentials, no DB."""

import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector import cloud_inventory as ci
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from db.models import AIAsset

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
CLOCK = 1_792_065_600
TOKEN = "ya29.vertex-access-token"
REGION = "europe-west4"
PROJECT = "acme-ml-prod"
SA_EMAIL = "ps-inventory@acme-ml-prod.iam.gserviceaccount.com"
KEY_ID = "0123456789abcdef-key-id"
P = f"projects/123456789012/locations/{REGION}"
VERTEX = f"{REGION}-aiplatform.googleapis.com"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = _PRIVATE_KEY.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
).decode()
# A distinctive slice of the key body, to check it never leaks.
SECRET = PEM.splitlines()[5]


async def _no_sleep(_s: float) -> None:
    return None


def _clock() -> float:
    return float(CLOCK)


class FakeSession:
    """Just enough of AsyncSession for store_inventory."""

    def __init__(self):
        self.rows: dict[uuid.UUID, AIAsset] = {}

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
        return None


class _NoRows:
    def scalars(self):
        return iter(())

    def all(self):
        return []


def _key(**kw) -> dict:
    return {
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": KEY_ID,
        "private_key": PEM,
        "client_email": SA_EMAIL,
        "token_uri": "https://evil.example/token",
        **kw,
    }


def _creds(**kw) -> ci.GcpInventoryCredentials:
    return ci.GcpInventoryCredentials(
        **{"service_account": _key(), "regions": [REGION], **kw}
    )


def _settings(**kw) -> ci.CloudInventorySettings:
    return ci.CloudInventorySettings(
        cloud_inventory={str(TENANT): ci.TenantCloudCredentials(gcp=_creds(**kw))}
    )


def _b64decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _check_assertion(assertion: str) -> dict:
    """The JWT is RS256-signed by the key and claims what Google requires."""
    header, claims, signature = assertion.split(".")
    _PRIVATE_KEY.public_key().verify(
        _b64decode(signature),
        f"{header}.{claims}".encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert json.loads(_b64decode(header)) == {
        "alg": "RS256",
        "typ": "JWT",
        "kid": KEY_ID,
    }
    return json.loads(_b64decode(claims))


TUNING_JOBS = {
    "tuningJobs": [
        {
            "name": f"{P}/tuningJobs/1",
            "state": "JOB_STATE_SUCCEEDED",
            "baseModel": "gemini-2.0-flash-001",
            "tunedModel": {
                "model": f"{P}/models/111@1",
                "endpoint": f"{P}/endpoints/900",
            },
            "labels": {"env": "prod", "department": "hr"},
        },
        {
            "name": f"{P}/tuningJobs/2",
            "state": "JOB_STATE_FAILED",
            "baseModel": "gemini-2.0-flash-001",
            "tunedModel": {"model": f"{P}/models/112@1"},
        },
        {
            # Another region's model is not trusted here.
            "name": f"{P}/tuningJobs/3",
            "state": "JOB_STATE_SUCCEEDED",
            "baseModel": "gemini-2.0-flash-001",
            "tunedModel": {"model": "projects/1/locations/us-central1/models/9"},
        },
    ]
}
MODELS = {
    "models": [
        {
            "name": f"{P}/models/111",
            "displayName": "tuned-hr-assistant",
            "labels": {"owner": "ml-team"},
        },
        {
            "name": f"{P}/models/222",
            "displayName": "llama3_1-mg-one-click-deploy",
            "baseModelSource": {
                "modelGardenSource": {
                    "publicModelName": "publishers/meta/models/llama3_1"
                }
            },
        },
        {"name": f"{P}/models/333", "displayName": "churn-classifier"},
        {"name": "projects/1/locations/us-central1/models/444", "displayName": "x"},
    ]
}
ENDPOINTS_PAGE_1 = {
    "endpoints": [
        {
            "name": f"{P}/endpoints/900",
            "deployedModels": [{"id": "1001", "model": f"{P}/models/111"}],
        },
        {
            "name": f"{P}/endpoints/901",
            "labels": {"environment": "staging", "business_unit": "risk"},
            "deployedModels": [
                {"id": "2001", "model": f"{P}/models/222@1"},
                {"id": "2002", "model": f"{P}/models/333"},
            ],
        },
    ],
    "nextPageToken": "page/2+=",
}
ENDPOINTS_PAGE_2 = {
    "endpoints": [
        {
            "name": f"{P}/endpoints/../../evil",
            "deployedModels": [{"id": "1", "model": f"{P}/models/333"}],
        },
        {"name": f"{P}/endpoints/903"},
        {
            "name": f"{P}/endpoints/904",
            "deployedModels": [
                {"id": "bad id!", "model": f"{P}/models/333"},
                {
                    "id": "4001",
                    "model": f"projects/999/locations/{REGION}/models/555",
                    "displayName": "shared-model",
                },
            ],
        },
    ]
}


def _vertex_handler(
    calls: list[httpx.Request], *, throttle_models: bool = False, state=None
):
    state = state if state is not None else {}
    state["throttled"] = not throttle_models

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com":
            assert request.method == "POST" and url.path == "/token"
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
            claims = _check_assertion(form["assertion"])
            iat = claims["iat"]
            assert claims == {
                "iss": SA_EMAIL,
                "scope": "https://www.googleapis.com/auth/cloud-platform",
                "aud": "https://oauth2.googleapis.com/token",
                "iat": iat,
                "exp": iat + 600,
            }
            state["iat"] = iat
            return httpx.Response(200, json={"access_token": TOKEN})

        assert url.host == VERTEX
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert url.params["pageSize"] == "100"
        base = f"/v1/projects/{PROJECT}/locations/{REGION}"
        if url.path == f"{base}/tuningJobs":
            return httpx.Response(200, json=TUNING_JOBS)
        if url.path == f"{base}/models":
            if not state["throttled"]:
                state["throttled"] = True
                return httpx.Response(429, headers={"retry-after": "2"})
            return httpx.Response(200, json=MODELS)
        if url.path == f"{base}/endpoints":
            if url.params.get("pageToken") == "page/2+=":
                return httpx.Response(200, json=ENDPOINTS_PAGE_2)
            assert "pageToken" not in url.params
            return httpx.Response(200, json=ENDPOINTS_PAGE_1)
        raise AssertionError(f"unexpected request {url}")

    return handler


async def test_vertex_lists_deployed_and_tuned_models():
    calls: list[httpx.Request] = []
    state: dict = {}
    summary, deps = await ci.fetch_gcp_deployments(
        _creds(),
        transport=httpx.MockTransport(
            _vertex_handler(calls, throttle_models=True, state=state)
        ),
        sleep=_no_sleep,
        clock=_clock,
    )
    assert summary == {"projects": 1, "regions": 1}
    assert state["iat"] == CLOCK
    assert [(d.resource_id, d.region, d.model, d.model_format) for d in deps] == [
        (f"{P}/models/111", REGION, "gemini-2.0-flash-001", "tuned_model"),
        (
            f"{P}/endpoints/900/deployedModels/1001",
            REGION,
            "gemini-2.0-flash-001",
            "tuned",
        ),
        (f"{P}/endpoints/901/deployedModels/2001", REGION, "llama3_1", "model_garden"),
        (
            f"{P}/endpoints/901/deployedModels/2002",
            REGION,
            "churn-classifier",
            "custom",
        ),
        (f"{P}/endpoints/904/deployedModels/4001", REGION, "shared-model", "custom"),
    ]
    # Tuned model: the registry model's labels on top of the tuning job's.
    assert deps[0].tags == {"env": "prod", "department": "hr", "owner": "ml-team"}
    assert deps[1].tags == {"owner": "ml-team"}
    assert deps[2].tags == {"environment": "staging", "business_unit": "risk"}
    # Only the fixed token host and the region's Vertex AI host; one POST.
    assert {c.url.host for c in calls} == {"oauth2.googleapis.com", VERTEX}
    assert [c.method for c in calls].count("POST") == 1


async def test_a_failing_region_does_not_stop_the_others():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        if request.url.host == "us-central1-aiplatform.googleapis.com":
            return httpx.Response(403, json={"error": {"message": "API disabled"}})
        return _vertex_handler([])(request)

    summary, deps = await ci.fetch_gcp_deployments(
        _creds(regions=[REGION, "us-central1"]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        clock=_clock,
    )
    assert summary == {
        "projects": 1,
        "regions": 2,
        "region_errors": {
            f"{PROJECT}/us-central1": "gcp: tuning job list returned HTTP 403"
        },
    }
    assert len(deps) == 5


async def test_every_region_failing_is_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(403)

    with pytest.raises(ci.CloudInventoryError, match="HTTP 403"):
        await ci.fetch_gcp_deployments(
            _creds(), transport=httpx.MockTransport(handler), sleep=_no_sleep
        )


US = "us-central1"
P_US = f"projects/123456789012/locations/{US}"
GARDEN_US = {
    "name": f"{P_US}/models/777",
    "displayName": "gemma3-one-click",
    "labels": {"owner": "us-team"},
    "baseModelSource": {
        "modelGardenSource": {"publicModelName": "publishers/google/models/gemma3"}
    },
}


def _cross_region_handler(calls: list[httpx.Request], *, model_status: int = 200):
    """An endpoint in europe-west4 serving a model registered in
    us-central1."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if url.host == VERTEX:
            if url.path.endswith("/endpoints"):
                dm = {"id": "1", "model": f"{P_US}/models/777@2", "displayName": "dm"}
                return httpx.Response(
                    200,
                    json={
                        "endpoints": [
                            {"name": f"{P}/endpoints/950", "deployedModels": [dm]}
                        ]
                    },
                )
            return httpx.Response(200, json={})
        assert url.host == f"{US}-aiplatform.googleapis.com"
        if url.path == f"/v1/projects/{PROJECT}/locations/{US}/models":
            return httpx.Response(200, json={"models": [GARDEN_US]})
        if url.path == f"/v1/{P_US}/models/777":
            return httpx.Response(model_status, json=GARDEN_US)
        return httpx.Response(200, json={})

    return handler


async def test_deployed_model_in_another_configured_region_is_named():
    calls: list[httpx.Request] = []
    summary, deps = await ci.fetch_gcp_deployments(
        _creds(regions=[REGION, US]),
        transport=httpx.MockTransport(_cross_region_handler(calls)),
        sleep=_no_sleep,
        clock=_clock,
    )
    assert summary == {"projects": 1, "regions": 2}
    assert [(d.resource_id, d.region, d.model, d.model_format) for d in deps] == [
        (f"{P}/endpoints/950/deployedModels/1", REGION, "gemma3", "model_garden")
    ]
    assert deps[0].tags == {"owner": "us-team"}
    # Named from the us-central1 registry listing: no extra lookup.
    assert f"/v1/{P_US}/models/777" not in [c.url.path for c in calls]


async def test_deployed_model_in_an_unlisted_region_is_looked_up():
    calls: list[httpx.Request] = []
    summary, deps = await ci.fetch_gcp_deployments(
        _creds(),
        transport=httpx.MockTransport(_cross_region_handler(calls)),
        sleep=_no_sleep,
        clock=_clock,
    )
    assert summary == {"projects": 1, "regions": 1}
    assert [(d.region, d.model, d.model_format) for d in deps] == [
        (REGION, "gemma3", "model_garden")
    ]
    assert [c.url.path for c in calls].count(f"/v1/{P_US}/models/777") == 1


async def test_failed_model_lookup_keeps_the_deployment():
    summary, deps = await ci.fetch_gcp_deployments(
        _creds(),
        transport=httpx.MockTransport(_cross_region_handler([], model_status=403)),
        sleep=_no_sleep,
        clock=_clock,
    )
    assert summary == {"projects": 1, "regions": 1}
    assert [(d.region, d.model, d.model_format) for d in deps] == [
        (REGION, "dm", "custom")
    ]


async def test_continuous_tuning_takes_the_base_model_from_the_pre_tuned_model():
    jobs = [
        {
            # Continued from tuned model 111; listed before the job that made it.
            "state": "JOB_STATE_SUCCEEDED",
            "baseModel": "",
            "preTunedModel": {"tunedModelName": f"{P}/models/111", "checkpointId": "2"},
            "tunedModel": {"model": f"{P}/models/113@1"},
        },
        {
            "state": "JOB_STATE_SUCCEEDED",
            "baseModel": "gemini-2.0-flash-001",
            "tunedModel": {"model": f"{P}/models/111@1"},
        },
        {
            # The pre-tuned model's own base model is reported.
            "state": "JOB_STATE_SUCCEEDED",
            "preTunedModel": {
                "tunedModelName": f"{P_US}/models/9",
                "baseModel": "gemini-2.5-flash",
            },
            "tunedModel": {"model": f"{P}/models/114"},
        },
        {
            # A cycle resolves to nothing rather than looping.
            "state": "JOB_STATE_SUCCEEDED",
            "preTunedModel": {"tunedModelName": f"{P}/models/115"},
            "tunedModel": {"model": f"{P}/models/115"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": TOKEN})
        if url.path.endswith("/tuningJobs"):
            return httpx.Response(200, json={"tuningJobs": jobs})
        if url.path.endswith("/endpoints"):
            dm = {"id": "1", "model": f"{P}/models/113@1"}
            ep = {"name": f"{P}/endpoints/960", "deployedModels": [dm]}
            return httpx.Response(200, json={"endpoints": [ep]})
        return httpx.Response(200, json={})

    _, deps = await ci.fetch_gcp_deployments(
        _creds(), transport=httpx.MockTransport(handler), sleep=_no_sleep, clock=_clock
    )
    assert [(d.resource_id, d.model, d.model_format) for d in deps] == [
        (f"{P}/models/113", "gemini-2.0-flash-001", "tuned_model"),
        (f"{P}/models/111", "gemini-2.0-flash-001", "tuned_model"),
        (f"{P}/models/114", "gemini-2.5-flash", "tuned_model"),
        (f"{P}/models/115", None, "tuned_model"),
        (f"{P}/endpoints/960/deployedModels/1", "gemini-2.0-flash-001", "tuned"),
    ]


async def test_access_token_is_renewed_before_it_expires():
    now = [float(CLOCK)]

    def clock() -> float:
        now[0] += 120.0
        return now[0]

    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            state["n"] += 1
            state["token"] = f"ya29.token-{state['n']}"
            return httpx.Response(
                200, json={"access_token": state["token"], "expires_in": 600}
            )
        # Always the latest token.
        assert request.headers["authorization"] == f"Bearer {state['token']}"
        return httpx.Response(200, json={})

    summary, _ = await ci.fetch_gcp_deployments(
        _creds(regions=[REGION, US, "europe-west1", "asia-east1"]),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        clock=clock,
    )
    assert summary == {"projects": 1, "regions": 4}
    assert state["n"] >= 3


async def test_pull_stores_assets_and_is_idempotent():
    db = FakeSession()
    kw = {
        "settings": _settings(),
        "transport": httpx.MockTransport(_vertex_handler([])),
        "sleep": _no_sleep,
    }
    first = await ci.pull_cloud_inventory(db, TENANT, clouds=["gcp"], now=NOW, **kw)
    assert first["clouds"]["gcp"]["assets_created"] == 5
    asset = next(
        a
        for a in db.rows.values()
        if a.calling_service.endswith("/deployedModels/2001")
    )
    asset.owner_email = "curated@acme.com"
    asset.discovery_source = ["sdk"]
    asset.lifecycle_state = "stale"
    result = await ci.pull_cloud_inventory(
        db, TENANT, clouds=["gcp"], now=NOW + timedelta(days=40), **kw
    )
    assert result["clouds"] == {
        "gcp": {
            "status": "ok",
            "projects": 1,
            "regions": 1,
            "deployments": 5,
            "assets_created": 0,
            "assets_updated": 5,
        }
    }
    assert len(db.rows) == 5
    assert {a.vendor for a in db.rows.values()} == {"google"}
    assert {a.region for a in db.rows.values()} == {REGION}
    assert asset.calling_service == f"gcp:{P}/endpoints/901/deployedModels/2001"
    assert (asset.model, asset.environment, asset.business_unit) == (
        "llama3_1",
        "staging",
        "risk",
    )
    assert asset.owner_email == "curated@acme.com"
    assert asset.discovery_source == ["sdk", "platform_signal"]
    assert asset.confidence == "verified"
    assert asset.lifecycle_state == "active"
    tuned = db.rows[
        ci.asset_id_for(
            TENANT, ci.CloudDeployment("gcp", f"{P}/models/111", None, None, None)
        )
    ]
    assert (tuned.environment, tuned.business_unit, tuned.confidence) == (
        "production",
        "hr",
        "medium",
    )
    # GCP label values cannot hold "@", so owner stays for the registry.
    assert tuned.owner_email is None


def test_asset_ids_are_scoped_by_cloud_and_tenant():
    gcp = ci.CloudDeployment("gcp", f"{P}/models/1", REGION, "m", None)
    azure = ci.CloudDeployment("azure", f"{P}/models/1", REGION, "m", None)
    assert ci.asset_id_for(TENANT, gcp) == ci.asset_id_for(TENANT, gcp)
    assert ci.asset_id_for(TENANT, gcp) != ci.asset_id_for(TENANT, azure)
    assert ci.asset_id_for(TENANT, gcp) != ci.asset_id_for(uuid.uuid4(), gcp)


async def test_errors_and_logs_never_contain_the_key(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(
            400, json={"error": "invalid_grant", "echo": request.content.decode()}
        )

    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        settings=_settings(),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert result["clouds"]["gcp"] == {
        "status": "error",
        "error": "gcp: token request returned HTTP 400",
    }
    blob = json.dumps(result) + caplog.text + repr(_settings()) + str(_settings())
    assert SECRET not in blob
    assert KEY_ID not in blob


async def test_unusable_private_key_is_reported_without_echo():
    broken = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key-SECRET\n-----END PRIVATE KEY-----\n"
    creds = _creds(service_account=_key(private_key=broken))

    def handler(request):
        raise AssertionError("no call expected")

    with pytest.raises(ci.CloudInventoryError) as exc_info:
        await ci.fetch_gcp_deployments(creds, transport=httpx.MockTransport(handler))
    assert "not-a-real-key" not in str(exc_info.value)


def test_credentials_come_from_env_with_key_file_as_string(monkeypatch):
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY",
        json.dumps(
            {
                str(TENANT): {
                    "gcp": {
                        "service_account": json.dumps(_key()),
                        "regions": [REGION, REGION],
                    }
                }
            }
        ),
    )
    creds = ci.credentials_for_tenant(TENANT).gcp
    # The project defaults to the key's own; regions are de-duplicated.
    assert creds.project_ids == [PROJECT]
    assert creds.regions == [REGION]
    assert creds.service_account.private_key.get_secret_value() == PEM
    assert ci.credentials_for_tenant(uuid.uuid4()).gcp is None


@pytest.mark.parametrize(
    "gcp",
    [
        {"regions": ["evil.example/x"]},
        {"regions": ["us-central1.evil.example"]},
        {"regions": ["global"]},
        {"regions": ["eu-central-1"]},
        {"regions": []},
        {"project_ids": ["Bad_Project"]},
        {"project_ids": ["acme/../x"]},
        {"service_account": _key(project_id=None)},
        {"service_account": _key(client_email="x@evil.example")},
        {"service_account": _key(type="authorized_user")},
        {"service_account": json.dumps(_key())[:-5]},
        {"service_account": SECRET},
    ],
)
def test_malformed_env_is_rejected_without_echoing_the_key(monkeypatch, gcp):
    value = {"service_account": _key(), "regions": [REGION], **gcp}
    monkeypatch.setenv("PS_CLOUD_INVENTORY", json.dumps({str(TENANT): {"gcp": value}}))
    with pytest.raises(Exception) as exc_info:
        ci.credentials_for_tenant(TENANT)
    assert SECRET not in str(exc_info.value)


def test_model_ids_from_publisher_names():
    assert ci._vertex_model_id("publishers/google/models/gemma2") == "gemma2"
    assert ci._vertex_model_id("gemini-2.0-flash-001") == "gemini-2.0-flash-001"
    assert ci._vertex_model_id("https://evil.example/x y") is None
    assert ci._vertex_model_id(None) is None


def test_request_body_cannot_carry_gcp_credentials():
    body = ci.CloudInventoryPullBody.model_validate(
        {"tenant_id": str(TENANT), "gcp": {"service_account": _key()}}
    )
    assert not hasattr(body, "gcp")
