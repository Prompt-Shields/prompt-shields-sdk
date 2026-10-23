"""Cloud AI inventory (platform signal), AWS Bedrock. HTTP is mocked; no
network, no credentials, no DB."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from collector import cloud_inventory as ci
from db.models import AIAsset

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
KEY_ID = "AKIAEXAMPLEKEY12345"
SECRET = "aws-SECRET-access-key-value"
ROLE_KEY_ID = "ASIAROLEKEY123456789"
ROLE_SECRET = "assumed-role-SECRET"
ROLE_TOKEN = "assumed-role-SESSION-token"
EXTERNAL_ID = "ps-external-id-42"
ACCT = "123456789012"
ROLE_ARN = f"arn:aws:iam::{ACCT}:role/PromptShieldsInventory"
FM = "arn:aws:bedrock:eu-central-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0"
CUSTOM = f"arn:aws:bedrock:eu-central-1:{ACCT}:custom-model/amazon.titan-text-express-v1:0:8k/abc123"
IMPORTED = f"arn:aws:bedrock:eu-central-1:{ACCT}:imported-model/llama-ft"
PT_FM = f"arn:aws:bedrock:eu-central-1:{ACCT}:provisioned-model/pt-haiku"
PT_CUSTOM = f"arn:aws:bedrock:eu-central-1:{ACCT}:provisioned-model/pt-custom"
PROFILE = f"arn:aws:bedrock:eu-central-1:{ACCT}:application-inference-profile/hr-bot"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
CLOCK = datetime(2026, 10, 15, 11, 59, 30, tzinfo=UTC)


async def _no_sleep(_s: float) -> None:
    return None


def _clock() -> datetime:
    return CLOCK


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


def _creds(**kw) -> ci.AwsInventoryCredentials:
    return ci.AwsInventoryCredentials(
        **{
            "access_key_id": KEY_ID,
            "secret_access_key": SECRET,
            "regions": ["eu-central-1"],
            **kw,
        }
    )


def _settings(**kw) -> ci.CloudInventorySettings:
    return ci.CloudInventorySettings(
        cloud_inventory={str(TENANT): ci.TenantCloudCredentials(aws=_creds(**kw))}
    )


def _check_signature(request: httpx.Request, keys: ci.AwsKeys) -> None:
    """The request carries a valid SigV4 signature over what was sent."""
    host = request.url.host
    region, service = host.split(".")[1], host.split(".")[0]
    query = request.url.query.decode()
    expected = ci.sigv4_headers(
        request.method,
        host,
        # As sent (still percent-encoded), not url.path, which is decoded.
        request.url.raw_path.decode().split("?", 1)[0],
        query,
        request.content,
        region=region,
        service=service,
        keys=keys,
        now=datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%S%z"),
        content_type=request.headers.get("content-type"),
    )
    assert request.headers["authorization"] == expected["authorization"]
    if keys.session_token:
        assert request.headers["x-amz-security-token"] == keys.session_token
    else:
        assert "x-amz-security-token" not in request.headers


def _aws_handler(
    calls: list[httpx.Request],
    *,
    keys: ci.AwsKeys | None = None,
    throttle_first: bool = False,
    failing_regions: tuple[str, ...] = (),
):
    keys = keys or ci.AwsKeys(KEY_ID, SECRET)
    state = {"throttled": not throttle_first}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        host, path = request.url.host, request.url.path
        if host.startswith("sts."):
            _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["Action"] == "AssumeRole"
            assert form["RoleArn"] == ROLE_ARN
            assert form.get("ExternalId") == EXTERNAL_ID
            return httpx.Response(
                200,
                text=(
                    '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                    "<AssumeRoleResult><Credentials>"
                    f"<AccessKeyId>{ROLE_KEY_ID}</AccessKeyId>"
                    f"<SecretAccessKey>{ROLE_SECRET}</SecretAccessKey>"
                    f"<SessionToken>{ROLE_TOKEN}</SessionToken>"
                    "<Expiration>2026-10-15T12:15:00Z</Expiration>"
                    "</Credentials></AssumeRoleResult></AssumeRoleResponse>"
                ),
            )

        region = host.split(".")[1]
        assert host == f"bedrock.{region}.amazonaws.com"
        _check_signature(request, keys)
        if region in failing_regions:
            return httpx.Response(403, json={"message": f"denied {SECRET}"})
        params = request.url.params
        if path == "/custom-models":
            assert request.method == "GET"
            return httpx.Response(
                200,
                json={
                    "modelSummaries": [
                        {
                            "modelArn": CUSTOM,
                            "modelName": "hr-titan",
                            "baseModelArn": (
                                "arn:aws:bedrock:eu-central-1::foundation-model/"
                                "amazon.titan-text-express-v1:0:8k"
                            ),
                            "modelStatus": "Active",
                        },
                        {
                            "modelArn": CUSTOM.replace("abc123", "bad1"),
                            "modelStatus": "Failed",
                        },
                        # Still training: not an asset yet.
                        {
                            "modelArn": CUSTOM.replace("abc123", "training1"),
                            "baseModelArn": FM,
                            "modelStatus": "Creating",
                        },
                        # Another region's ARN is never stored for this one.
                        {"modelArn": CUSTOM.replace("eu-central-1", "us-east-1")},
                    ]
                },
            )
        if path == "/imported-models":
            if not state["throttled"]:
                state["throttled"] = True
                return httpx.Response(429, headers={"retry-after": "2"})
            if params.get("nextToken") != "page/2+==":
                return httpx.Response(
                    200, json={"modelSummaries": [], "nextToken": "page/2+=="}
                )
            return httpx.Response(
                200,
                json={
                    "modelSummaries": [{"modelArn": IMPORTED, "modelName": "llama-ft"}]
                },
            )
        if path == "/provisioned-model-throughputs":
            return httpx.Response(
                200,
                json={
                    "provisionedModelSummaries": [
                        {
                            "provisionedModelArn": PT_FM,
                            "modelArn": FM,
                            "foundationModelArn": FM,
                            "status": "InService",
                        },
                        {
                            "provisionedModelArn": PT_CUSTOM,
                            "modelArn": CUSTOM,
                            "foundationModelArn": "not-an-arn",
                            "status": "Creating",
                        },
                        {
                            "provisionedModelArn": PT_FM + "-x",
                            "foundationModelArn": FM,
                            "status": "Failed",
                        },
                    ]
                },
            )
        if path == "/inference-profiles":
            assert params.get("type") == "APPLICATION"
            assert "typeEquals" not in params
            return httpx.Response(
                200,
                json={
                    "inferenceProfileSummaries": [
                        {
                            "inferenceProfileArn": PROFILE,
                            "status": "ACTIVE",
                            "type": "APPLICATION",
                            "models": [{"modelArn": FM}],
                        },
                        {
                            "inferenceProfileArn": "arn:aws:bedrock:eu-central-1:"
                            f"{ACCT}:inference-profile/eu.anthropic.claude",
                            "status": "ACTIVE",
                        },
                    ]
                },
            )
        if path == "/listTagsForResource":
            assert request.method == "POST"
            arn = json.loads(request.content)["resourceARN"]
            tags = {
                CUSTOM: {"Owner": "ml@acme.com", "env": "prod", "Department": "HR"},
                PT_CUSTOM: {"Owner": "platform@acme.com"},
                PROFILE: {"owner": "hr-bot@acme.com", "environment": "staging"},
            }.get(arn, {})
            return httpx.Response(
                200, json={"tags": [{"key": k, "value": v} for k, v in tags.items()]}
            )
        if path == "/logging/modelinvocations":
            return httpx.Response(
                200,
                json={"loggingConfig": {"s3Config": {"bucketName": "logs"}}},
            )
        raise AssertionError(f"unexpected request {request.url}")

    return handler


async def _fetch(creds, handler):
    return await ci.fetch_aws_deployments(
        creds,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        clock=_clock,
    )


def test_sigv4_matches_aws_test_suite_vector():
    # "get-vanilla" from the AWS Signature Version 4 test suite.
    headers = ci.sigv4_headers(
        "GET",
        "example.amazonaws.com",
        "/",
        "",
        b"",
        region="us-east-1",
        service="service",
        keys=ci.AwsKeys("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"),
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
    )
    assert headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/"
        "aws4_request, SignedHeaders=host;x-amz-date, "
        "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


async def test_bedrock_resources_in_use_with_region_and_tags():
    calls: list[httpx.Request] = []
    summary, deps = await _fetch(_creds(), _aws_handler(calls, throttle_first=True))
    assert summary == {"regions": 1}
    assert [(d.resource_id, d.model, d.model_format, d.region) for d in deps] == [
        (CUSTOM, "amazon.titan-text-express-v1:0:8k", "custom", "eu-central-1"),
        (IMPORTED, "llama-ft", "imported", "eu-central-1"),
        (
            PT_FM,
            "anthropic.claude-3-haiku-20240307-v1:0",
            "provisioned",
            "eu-central-1",
        ),
        (PT_CUSTOM, "amazon.titan-text-express-v1:0:8k", "provisioned", "eu-central-1"),
        (
            PROFILE,
            "anthropic.claude-3-haiku-20240307-v1:0",
            "inference_profile",
            "eu-central-1",
        ),
    ]
    # A provisioned custom model inherits the model's tags; its own win.
    assert deps[3].tags == {
        "Owner": "platform@acme.com",
        "env": "prod",
        "Department": "HR",
    }
    assert {c.url.host for c in calls} == {"bedrock.eu-central-1.amazonaws.com"}
    assert {c.headers["x-amz-date"] for c in calls} == {"20261015T115930Z"}
    # Only reads: list calls are GET, and the one POST is ListTagsForResource.
    assert {c.url.path for c in calls if c.method == "POST"} == {"/listTagsForResource"}


async def test_assume_role_with_external_id_uses_session_credentials():
    calls: list[httpx.Request] = []
    creds = _creds(
        role_arn=ROLE_ARN,
        external_id=EXTERNAL_ID,
        regions=["eu-central-1", "us-east-1"],
    )
    role_keys = ci.AwsKeys(ROLE_KEY_ID, ROLE_SECRET, ROLE_TOKEN)
    summary, deps = await _fetch(creds, _aws_handler(calls, keys=role_keys))
    assert calls[0].url.host == "sts.eu-central-1.amazonaws.com"
    assert [c.url.host for c in calls].count("sts.eu-central-1.amazonaws.com") == 1
    assert "bedrock.us-east-1.amazonaws.com" in {c.url.host for c in calls}
    assert summary == {"regions": 2}
    # The us-east-1 copy of the custom model ARN is stored once, for us-east-1.
    assert [d.region for d in deps if "us-east-1" in d.resource_id] == ["us-east-1"]


def _sts_response(keys: ci.AwsKeys, expiration: datetime) -> httpx.Response:
    return httpx.Response(
        200,
        text=(
            '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
            "<AssumeRoleResult><Credentials>"
            f"<AccessKeyId>{keys.access_key_id}</AccessKeyId>"
            f"<SecretAccessKey>{keys.secret_access_key}</SecretAccessKey>"
            f"<SessionToken>{keys.session_token}</SessionToken>"
            f"<Expiration>{expiration.strftime('%Y-%m-%dT%H:%M:%SZ')}</Expiration>"
            "</Credentials></AssumeRoleResult></AssumeRoleResponse>"
        ),
    )


def _amz_date(request: httpx.Request) -> datetime:
    return datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%S%z")


async def test_govcloud_assumes_the_role_in_its_own_partition():
    gov_role = f"arn:aws-us-gov:iam::{ACCT}:role/Inventory"
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.host.startswith("sts."):
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["RoleArn"] == gov_role
            assert form["DurationSeconds"] == "3600"
            return _sts_response(
                ci.AwsKeys(ROLE_KEY_ID, ROLE_SECRET, ROLE_TOKEN),
                _amz_date(request) + timedelta(hours=1),
            )
        _check_signature(request, ci.AwsKeys(ROLE_KEY_ID, ROLE_SECRET, ROLE_TOKEN))
        return httpx.Response(200, json={})

    summary, _ = await _fetch(
        _creds(role_arn=gov_role, regions=["us-gov-west-1", "us-gov-east-1"]), handler
    )
    assert summary == {"regions": 2}
    assert hosts[0] == "sts.us-gov-west-1.amazonaws.com"
    assert hosts.count(hosts[0]) == 1
    assert {h for h in hosts if h.startswith("bedrock.")} == {
        "bedrock.us-gov-west-1.amazonaws.com",
        "bedrock.us-gov-east-1.amazonaws.com",
    }


async def test_assumed_role_keys_are_renewed_before_they_expire():
    # Every clock read moves time on by a minute, so 15-minute sessions run
    # out several times during one inventory of five regions.
    times = iter(CLOCK + timedelta(minutes=i) for i in range(10_000))
    state: dict = {"sts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        date = _amz_date(request)
        if request.url.host.startswith("sts."):
            _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
            form = dict(httpx.QueryParams(request.content.decode()))
            assert form["DurationSeconds"] == "900"
            state["sts"] += 1
            n = state["sts"]
            state["keys"] = ci.AwsKeys(f"ASIAROLE{n:012d}", f"secret-{n}", f"token-{n}")
            state["expires"] = date + timedelta(minutes=15)
            return _sts_response(state["keys"], state["expires"])
        # Signed with the latest session's keys, before they expire.
        _check_signature(request, state["keys"])
        assert date < state["expires"]
        return httpx.Response(200, json={})

    regions = ["eu-central-1", "eu-west-1", "eu-west-2", "eu-north-1", "us-east-1"]
    summary, _ = await ci.fetch_aws_deployments(
        _creds(role_arn=ROLE_ARN, session_duration_seconds=900, regions=regions),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
        clock=lambda: next(times),
    )
    assert summary == {"regions": 5}
    assert state["sts"] >= 3


async def test_failing_region_is_reported_and_others_continue():
    calls: list[httpx.Request] = []
    summary, deps = await _fetch(
        _creds(regions=["eu-central-1", "eu-west-1"], check_invocation_logging=True),
        _aws_handler(calls, failing_regions=("eu-west-1",)),
    )
    assert summary == {
        "regions": 2,
        "region_errors": {
            "eu-west-1": "aws: custom model list (eu-west-1) returned HTTP 403"
        },
        "invocation_logging": {"eu-central-1": True},
    }
    assert len(deps) == 5


async def test_every_region_failing_is_an_error():
    with pytest.raises(ci.CloudInventoryError, match="HTTP 403"):
        await _fetch(_creds(), _aws_handler([], failing_regions=("eu-central-1",)))


async def test_pull_stores_aws_assets_and_is_idempotent():
    db = FakeSession()
    kw = {
        "settings": _settings(),
        "transport": httpx.MockTransport(_aws_handler([])),
        "sleep": _no_sleep,
    }
    for i in range(2):
        result = await ci.pull_cloud_inventory(
            db, TENANT, clouds=["aws"], now=NOW + timedelta(days=i), **kw
        )
    assert result["clouds"] == {
        "aws": {
            "status": "ok",
            "regions": 1,
            "deployments": 5,
            "assets_created": 0,
            "assets_updated": 5,
        }
    }
    assert len(db.rows) == 5
    profile = db.rows[
        ci.asset_id_for(TENANT, ci.CloudDeployment("aws", PROFILE, None, None, None))
    ]
    assert (
        profile.vendor,
        profile.model,
        profile.region,
        profile.owner_email,
        profile.environment,
        profile.discovery_source,
        profile.confidence,
        profile.calling_service,
        profile.first_seen,
        profile.last_seen,
    ) == (
        "amazon",
        "anthropic.claude-3-haiku-20240307-v1:0",
        "eu-central-1",
        "hr-bot@acme.com",
        "staging",
        ["platform_signal"],
        "medium",
        f"aws:{PROFILE}",
        NOW,
        NOW + timedelta(days=1),
    )


async def test_rerun_keeps_curated_owner():
    db = FakeSession()
    kw = {
        "settings": _settings(),
        "transport": httpx.MockTransport(_aws_handler([])),
        "sleep": _no_sleep,
    }
    await ci.pull_cloud_inventory(db, TENANT, clouds=["aws"], now=NOW, **kw)
    aid = ci.asset_id_for(TENANT, ci.CloudDeployment("aws", CUSTOM, None, None, None))
    db.rows[aid].owner_email = "curated@acme.com"
    await ci.pull_cloud_inventory(db, TENANT, clouds=["aws"], now=NOW, **kw)
    assert db.rows[aid].owner_email == "curated@acme.com"


def test_asset_ids_differ_per_cloud_and_tenant():
    d = ci.CloudDeployment("aws", PROFILE, "eu-central-1", "m", None)
    assert ci.asset_id_for(TENANT, d) == ci.asset_id_for(TENANT, d)
    assert ci.asset_id_for(TENANT, d) != ci.asset_id_for(uuid.uuid4(), d)
    assert ci.asset_id_for(TENANT, d) != ci.asset_id_for(
        TENANT, ci.CloudDeployment("azure", PROFILE, None, None, None)
    )


async def test_errors_logs_and_repr_never_contain_secrets(caplog):
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(
            403, text=f"<Error>{SECRET} {EXTERNAL_ID} {ROLE_TOKEN}</Error>"
        )

    settings = _settings(role_arn=ROLE_ARN, external_id=EXTERNAL_ID)
    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        settings=settings,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert result["clouds"]["aws"] == {
        "status": "error",
        "error": "aws: assume role returned HTTP 403",
    }
    blob = (
        json.dumps(result)
        + caplog.text
        + repr(settings)
        + repr(ci.AwsKeys(ROLE_KEY_ID, ROLE_SECRET, ROLE_TOKEN))
    )
    for secret in (SECRET, EXTERNAL_ID, ROLE_SECRET, ROLE_TOKEN):
        assert secret not in blob


async def test_assume_role_without_credentials_in_response_fails():
    def handler(request):
        return httpx.Response(200, text="<AssumeRoleResponse/>")

    with pytest.raises(ci.CloudInventoryError, match="no credentials"):
        await _fetch(_creds(role_arn=ROLE_ARN), handler)


_UNRESERVED = "-._~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


@pytest.mark.parametrize(
    "method,params,body,content_type,signature",
    [
        # Expected values from the AWS SigV4 test suite (also match botocore).
        ("POST", {}, b"", None, "5da7c1a2acd57cee7505fc6676e4e544621c30862966e37dddb68e92efbe5d6b"),
        (
            "GET",
            {"Param2": "value2", "Param1": "value1"},
            b"",
            None,
            "b97d918cfa904a5beff61c982a1b6f458b799221646efd99d3219ec94cdf2500",
        ),
        (
            "GET",
            {_UNRESERVED: _UNRESERVED},
            b"",
            None,
            "9c3e54bfcdf0b19771a7f523ee5669cdf59bc7cc0884027167c21bb143a40197",
        ),
        ("GET", {"ሴ": "bar"}, b"", None, "2cdec8eed098649ff3a119c94853b13c643bcf08f8b0a1d91e12c9027818dd04"),
        (
            "POST",
            {},
            b"Param1=value1",
            "application/x-www-form-urlencoded",
            "ff11897932ad3f4e8b18135d722051e5ac45fc38421b1da7b9d196a0fe09473a",
        ),
        (
            "POST",
            {},
            b"Param1=value1",
            "application/x-www-form-urlencoded; charset=utf8",
            "1a72ec8f64bd914b0e42e42607c7fbce7fb2c7465f63e3092b3b0d39fa77a6fe",
        ),
    ],
)
def test_sigv4_more_test_suite_vectors(method, params, body, content_type, signature):
    headers = ci.sigv4_headers(
        method,
        "example.amazonaws.com",
        "/",
        ci.canonical_query(params),
        body,
        region="us-east-1",
        service="service",
        keys=ci.AwsKeys("AKIDEXAMPLE", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"),
        now=datetime(2015, 8, 30, 12, 36, tzinfo=UTC),
        content_type=content_type,
    )
    assert headers["authorization"].endswith(f"Signature={signature}")


async def test_retries_are_signed_again():
    dates = []
    times = iter(CLOCK + timedelta(minutes=i) for i in range(100))

    def handler(request):
        dates.append(request.headers["x-amz-date"])
        _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
        return httpx.Response(503)

    with pytest.raises(ci.CloudInventoryError, match="retries"):
        await ci.fetch_aws_deployments(
            _creds(),
            transport=httpx.MockTransport(handler),
            sleep=_no_sleep,
            clock=lambda: next(times),
        )
    assert len(dates) == ci.MAX_RETRIES + 1
    assert len(set(dates)) == len(dates)


async def test_gives_up_after_max_retries():
    def handler(request):
        return httpx.Response(503)

    with pytest.raises(ci.CloudInventoryError, match="retries"):
        await _fetch(_creds(), handler)


def test_aws_credentials_come_from_env(monkeypatch):
    monkeypatch.setenv(
        "PS_CLOUD_INVENTORY",
        json.dumps(
            {
                str(TENANT): {
                    "aws": {
                        "access_key_id": KEY_ID,
                        "secret_access_key": SECRET,
                        "role_arn": ROLE_ARN,
                        "external_id": EXTERNAL_ID,
                        "regions": ["eu-central-1", "eu-central-1", "us-east-1"],
                    }
                }
            }
        ),
    )
    creds = ci.credentials_for_tenant(TENANT).aws
    assert creds.secret_access_key.get_secret_value() == SECRET
    assert creds.regions == ["eu-central-1", "us-east-1"]
    assert creds.session_duration_seconds == 3600
    assert ci.credentials_for_tenant(uuid.uuid4()).aws is None


@pytest.mark.parametrize(
    "aws",
    [
        {"regions": ["eu-central-1.evil.com"]},
        {"regions": ["evil.com/x"]},
        {"regions": ["eu-central-1@evil"]},
        {"regions": ["EU-CENTRAL-1"]},
        {"regions": ["cn-north-1"]},
        {"regions": []},
        {"access_key_id": "akia lower"},
        {"role_arn": "arn:aws:iam::123:role/x"},
        {"external_id": "x"},  # external_id without role_arn
        {"role_arn": ROLE_ARN, "external_id": "has space"},
        {"role_session_name": "a b"},
        # Keys, roles and STS are per partition: never mixed.
        {"regions": ["eu-central-1", "us-gov-west-1"]},
        {"regions": ["us-gov-west-1"], "role_arn": ROLE_ARN},
        {
            "regions": ["eu-central-1"],
            "role_arn": f"arn:aws-us-gov:iam::{ACCT}:role/Inventory",
        },
        {"role_arn": ROLE_ARN, "session_duration_seconds": 899},
        {"role_arn": ROLE_ARN, "session_duration_seconds": 43201},
    ],
)
def test_malformed_aws_env_is_rejected_without_echoing_the_secret(monkeypatch, aws):
    value = {
        "access_key_id": KEY_ID,
        "secret_access_key": SECRET,
        "regions": ["eu-central-1"],
        **aws,
    }
    monkeypatch.setenv("PS_CLOUD_INVENTORY", json.dumps({str(TENANT): {"aws": value}}))
    with pytest.raises(Exception) as exc_info:
        ci.credentials_for_tenant(TENANT)
    assert SECRET not in str(exc_info.value)


async def test_not_configured_aws_makes_no_calls():
    def handler(request):
        raise AssertionError("no call expected")

    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        settings=ci.CloudInventorySettings(
            cloud_inventory={str(TENANT): ci.TenantCloudCredentials()}
        ),
        transport=httpx.MockTransport(handler),
    )
    assert result["clouds"] == {
        "azure": {"status": "not_configured"},
        "aws": {"status": "not_configured"},
        "gcp": {"status": "not_configured"},
    }


def test_request_body_cannot_carry_aws_credentials():
    body = ci.CloudInventoryPullBody.model_validate(
        {"tenant_id": str(TENANT), "aws": {"secret_access_key": "x"}}
    )
    assert not hasattr(body, "aws")


# --- Bedrock model access (check_model_access) ------------------------------

HAIKU = "anthropic.claude-3-haiku-20240307-v1:0"
SONNET = "anthropic.claude-3-5-sonnet-20240620-v1:0"
TITAN = "amazon.titan-text-express-v1"
LLAMA = "meta.llama3-8b-instruct-v1:0"
OLD = "anthropic.claude-v2:1"


def _fm(model_id: str, types=("ON_DEMAND",), status="ACTIVE"):
    return {
        "modelArn": f"arn:aws:bedrock:eu-central-1::foundation-model/{model_id}",
        "modelId": model_id,
        "inferenceTypesSupported": list(types),
        "modelLifecycle": {"status": status},
    }


def _availability(authorized=True, agreement="AVAILABLE"):
    return {
        "agreementAvailability": {"status": agreement},
        "authorizationStatus": "AUTHORIZED" if authorized else "NOT_AUTHORIZED",
        "entitlementAvailability": "AVAILABLE",
        "regionAvailability": "AVAILABLE",
    }


def _system_profile(profile_id: str, model_id: str, regions, status="ACTIVE"):
    return {
        "inferenceProfileArn": (
            f"arn:aws:bedrock:eu-central-1:{ACCT}:inference-profile/{profile_id}"
        ),
        "inferenceProfileId": profile_id,
        "type": "SYSTEM_DEFINED",
        "status": status,
        "models": [
            {"modelArn": f"arn:aws:bedrock:{r}::foundation-model/{model_id}"}
            for r in regions
        ],
    }


def _model_access_handler(calls: list[httpx.Request], *, availability_status=200):
    resources = _aws_handler(calls)
    availability = {
        HAIKU: _availability(),
        SONNET: _availability(agreement="PENDING"),
        TITAN: _availability(authorized=False),
        OLD: _availability(),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/foundation-models":
            calls.append(request)
            _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
            return httpx.Response(
                200,
                json={
                    "modelSummaries": [
                        _fm(HAIKU, ("ON_DEMAND", "PROVISIONED")),
                        _fm(SONNET, ("INFERENCE_PROFILE",)),
                        _fm(TITAN),
                        _fm(OLD, status="LEGACY"),
                        # Unknown to the availability API: skipped.
                        _fm(LLAMA),
                        # Provisioned-only: inventoried as throughput instead.
                        _fm("amazon.titan-text-express-v1:0:8k", ("PROVISIONED",)),
                        # Malformed ids are never put in a URL.
                        _fm("../../evil"),
                        {"modelId": 7},
                        "junk",
                    ]
                },
            )
        if path.startswith("/foundation-model-availability/"):
            calls.append(request)
            _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
            # The id's ":" is sent percent-encoded, as the AWS SDKs do.
            assert ":" not in request.url.raw_path.decode()
            model_id = path.rsplit("/", 1)[1]
            if availability_status != 200:
                return httpx.Response(availability_status)
            if model_id not in availability:
                return httpx.Response(404)
            return httpx.Response(200, json=availability[model_id])
        if (
            path == "/inference-profiles"
            and request.url.params.get("type") == "SYSTEM_DEFINED"
        ):
            calls.append(request)
            _check_signature(request, ci.AwsKeys(KEY_ID, SECRET))
            return httpx.Response(
                200,
                json={
                    "inferenceProfileSummaries": [
                        _system_profile(
                            f"eu.{HAIKU}",
                            HAIKU,
                            ["eu-central-1", "eu-west-1", "eu-west-3"],
                        ),
                        # Routes to a model the account cannot use.
                        _system_profile(f"eu.{TITAN}", TITAN, ["eu-central-1"]),
                        _system_profile(
                            f"global.{OLD}", OLD, ["eu-central-1"], status="DISABLED"
                        ),
                        {"inferenceProfileArn": "not-an-arn", "status": "ACTIVE"},
                    ]
                },
            )
        return resources(request)

    return handler


async def test_model_access_is_reported_per_region():
    calls: list[httpx.Request] = []
    summary, deps = await _fetch(
        _creds(check_model_access=True), _model_access_handler(calls)
    )
    assert summary == {
        "regions": 1,
        "model_access": {
            "eu-central-1": {
                "foundation_models": [SONNET, HAIKU, OLD],
                "inference_profiles": {
                    f"eu.{HAIKU}": ["eu-central-1", "eu-west-1", "eu-west-3"]
                },
                "agreement_not_accepted": [SONNET],
            }
        },
    }
    # Model access is reported, not stored: the assets are unchanged.
    assert len(deps) == 5
    checked = sorted(
        c.url.path.rsplit("/", 1)[1]
        for c in calls
        if c.url.path.startswith("/foundation-model-availability/")
    )
    assert checked == sorted([HAIKU, SONNET, TITAN, OLD, LLAMA])
    # Still read-only: the one POST is ListTagsForResource.
    assert {c.url.path for c in calls if c.method == "POST"} == {"/listTagsForResource"}


async def test_model_access_is_off_by_default():
    calls: list[httpx.Request] = []
    summary, _ = await _fetch(_creds(), _model_access_handler(calls))
    assert "model_access" not in summary
    assert not any(c.url.path.startswith("/foundation-model") for c in calls)


async def test_model_access_denied_is_reported_apart_from_the_region():
    calls: list[httpx.Request] = []
    summary, deps = await _fetch(
        _creds(check_model_access=True),
        _model_access_handler(calls, availability_status=403),
    )
    assert summary == {
        "regions": 1,
        "model_access": {},
        "model_access_errors": {
            "eu-central-1": "aws: model availability (eu-central-1) returned HTTP 403"
        },
    }
    # The region's resources are still inventoried.
    assert len(deps) == 5
    # Stopped at the first refusal instead of trying every model.
    availability_calls = [
        c for c in calls if c.url.path.startswith("/foundation-model-availability/")
    ]
    assert len(availability_calls) == 1


async def test_model_access_flows_into_the_pull_result():
    result = await ci.pull_cloud_inventory(
        FakeSession(),
        TENANT,
        clouds=["aws"],
        settings=_settings(check_model_access=True),
        transport=httpx.MockTransport(_model_access_handler([])),
        sleep=_no_sleep,
        now=NOW,
    )
    aws = result["clouds"]["aws"]
    assert aws["status"] == "ok"
    assert aws["model_access"]["eu-central-1"]["foundation_models"] == [
        SONNET,
        HAIKU,
        OLD,
    ]


def test_sigv4_encodes_the_path_again_like_botocore():
    # Expected value from botocore 1.43 (SigV4Auth) signing
    # GetFoundationModelAvailability for this model id, keys and time.
    headers = ci.sigv4_headers(
        "GET",
        "bedrock.eu-central-1.amazonaws.com",
        "/foundation-model-availability/anthropic.claude-3-haiku-20240307-v1%3A0",
        "",
        b"",
        region="eu-central-1",
        service="bedrock",
        keys=ci.AwsKeys("AKIDEXAMPLE", "secretkey"),
        now=datetime(2026, 10, 23, 19, 28, 20, tzinfo=UTC),
    )
    assert headers["authorization"].endswith(
        "Signature=e4dda77579fc0fbae3270c514d126a523f453220f929e6ebceda74937fb25538"
    )
