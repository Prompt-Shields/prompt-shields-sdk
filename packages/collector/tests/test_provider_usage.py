"""Provider org usage pull (platform signal). HTTP is mocked; no network, no DB."""

import logging
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from collector import provider_usage as pu
from db.models import AIAsset, AIUsageEvent
from pydantic import SecretStr

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OPENAI_KEY = "sk-admin-openai-SECRET"
ANTHROPIC_KEY = "sk-ant-admin01-SECRET"
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 3, tzinfo=UTC)
DAY1 = int(START.timestamp())
DAY2 = DAY1 + 86400
DAY3 = DAY2 + 86400


async def _no_sleep(_seconds: float) -> None:
    return None


class _NoRows:
    def scalars(self):
        return iter(())


class FakeSession:
    """Just enough of AsyncSession for store_usage: get / add / flush / commit.
    `execute` (stored credentials, fingerprint links) finds nothing."""

    def __init__(self):
        self.rows: dict[tuple[type, uuid.UUID], object] = {}
        self.commits = 0

    async def get(self, model, ident):
        return self.rows.get((model, ident))

    def add(self, obj):
        self.rows[(type(obj), obj.id)] = obj

    async def execute(self, _stmt):
        return _NoRows()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    def of(self, model):
        return [v for (m, _), v in self.rows.items() if m is model]


def _settings(**keys):
    return pu.ProviderUsageSettings(provider_admin_keys={str(TENANT): keys})


def _openai_handler(calls: list[httpx.Request], *, rate_limit_first: bool = False):
    state = {"limited": not rate_limit_first}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if not state["limited"]:
            state["limited"] = True
            return httpx.Response(429, headers={"retry-after": "2"})
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "embeddings":
            return httpx.Response(
                200, json={"data": [], "has_more": False, "next_page": None}
            )
        page = request.url.params.get("page")
        if page is None:
            return httpx.Response(
                200,
                json={
                    "object": "page",
                    "data": [
                        {
                            "start_time": DAY1,
                            "end_time": DAY2,
                            "results": [
                                {
                                    "input_tokens": 100,
                                    "output_tokens": 50,
                                    "num_model_requests": 3,
                                    "project_id": "proj_a",
                                    "api_key_id": "key_shadow",
                                    "model": "gpt-4o-mini",
                                }
                            ],
                        }
                    ],
                    "has_more": True,
                    "next_page": "page_2",
                },
            )
        assert page == "page_2"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "start_time": DAY2,
                        "end_time": DAY3,
                        "results": [
                            {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "num_model_requests": 1,
                                "project_id": "proj_a",
                                "api_key_id": "key_shadow",
                                "model": "gpt-4o-mini",
                            }
                        ],
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    return handler


async def test_openai_paginates_retries_and_uses_fixed_host():
    calls: list[httpx.Request] = []
    transport = httpx.MockTransport(_openai_handler(calls, rate_limit_first=True))
    slept: list[float] = []

    async def sleep(s):
        slept.append(s)

    records = await pu.fetch_openai_usage(
        SecretStr(OPENAI_KEY), START, END, transport=transport, sleep=sleep
    )

    assert slept == [2.0]  # honoured Retry-After
    assert {r.url.host for r in calls} == {"api.openai.com"}
    assert all(r.headers["authorization"] == f"Bearer {OPENAI_KEY}" for r in calls)
    first = calls[1]
    assert first.url.params.get_list("group_by") == [
        "project_id",
        "api_key_id",
        "model",
    ]
    assert first.url.params["bucket_width"] == "1d"
    assert first.url.params["start_time"] == str(DAY1)
    assert [r.tokens_in for r in records] == [100, 10]
    assert records[0].project_id == "proj_a" and records[0].api_key_id == "key_shadow"


async def test_anthropic_parses_report_and_sends_admin_headers():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-10-01T00:00:00Z",
                        "ending_at": "2026-10-02T00:00:00Z",
                        "results": [
                            {
                                "uncached_input_tokens": 100,
                                "cache_read_input_tokens": 20,
                                "cache_creation": {
                                    "ephemeral_5m_input_tokens": 5,
                                    "ephemeral_1h_input_tokens": 1,
                                },
                                "output_tokens": 40,
                                "workspace_id": "wrkspc_x",
                                "api_key_id": "apikey_y",
                                "model": "claude-sonnet-4-5",
                            }
                        ],
                    }
                ],
                "has_more": False,
                "next_page": None,
            },
        )

    records = await pu.fetch_anthropic_usage(
        SecretStr(ANTHROPIC_KEY),
        START,
        END,
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )

    (req,) = calls
    assert req.url.host == "api.anthropic.com"
    assert req.url.path == "/v1/organizations/usage_report/messages"
    assert req.headers["x-api-key"] == ANTHROPIC_KEY
    assert req.headers["anthropic-version"] == pu.ANTHROPIC_VERSION
    assert req.url.params.get_list("group_by[]") == [
        "workspace_id",
        "api_key_id",
        "model",
    ]
    assert req.url.params["starting_at"] == "2026-10-01T00:00:00Z"
    (r,) = records
    assert (r.tokens_in, r.tokens_out) == (126, 40)
    assert (r.project_id, r.api_key_id, r.model) == (
        "wrkspc_x",
        "apikey_y",
        "claude-sonnet-4-5",
    )


async def test_pull_creates_medium_confidence_assets_and_is_idempotent():
    db = FakeSession()
    settings = _settings(openai=OPENAI_KEY)
    transport = httpx.MockTransport(_openai_handler([]))

    first = await pu.pull_provider_usage(
        db,
        TENANT,
        start=START,
        end=END,
        settings=settings,
        transport=transport,
        sleep=_no_sleep,
    )
    assert first["providers"]["openai"]["assets_created"] == 1
    assert first["providers"]["openai"]["events_created"] == 2
    assert first["providers"]["anthropic"] == {"status": "not_configured"}

    (asset,) = db.of(AIAsset)
    assert asset.discovery_source == ["platform_signal"]
    assert asset.confidence == "medium"
    assert asset.vendor == "openai" and asset.model == "gpt-4o-mini"
    assert asset.calling_service == "openai:project=proj_a:key=key_shadow"
    assert asset.first_seen == START
    assert asset.last_seen == datetime.fromtimestamp(DAY3, tz=UTC)

    # Re-running the same window must not double count.
    second = await pu.pull_provider_usage(
        db,
        TENANT,
        start=START,
        end=END,
        settings=settings,
        transport=transport,
        sleep=_no_sleep,
    )
    assert second["providers"]["openai"]["assets_created"] == 0
    assert second["providers"]["openai"]["events_created"] == 0
    assert second["providers"]["openai"]["events_updated"] == 2
    events = db.of(AIUsageEvent)
    assert len(db.of(AIAsset)) == 1
    assert len(events) == 2
    assert sum(e.tokens_in for e in events) == 110
    assert all(e.source == "platform_signal" for e in events)


async def test_existing_sdk_asset_is_corroborated():
    db = FakeSession()
    record = pu.UsageRecord(
        provider="openai",
        endpoint="completions",
        bucket_start=START,
        bucket_end=END,
        project_id="proj_a",
        api_key_id="key_1",
        model="gpt-4o",
        tokens_in=1,
        tokens_out=1,
        requests=1,
    )
    existing = AIAsset(
        id=pu.asset_id_for(TENANT, record),
        tenant_id=TENANT,
        vendor="openai",
        model="gpt-4o",
        discovery_source=["sdk"],
        confidence="high",
        first_seen=END,
        last_seen=END,
    )
    db.add(existing)
    await pu.store_usage(db, TENANT, [record])
    assert existing.discovery_source == ["sdk", "platform_signal"]
    assert existing.confidence == "verified"
    assert existing.first_seen == START


def test_aggregate_sums_duplicate_groups():
    r = pu.UsageRecord("anthropic", "messages", START, END, "w", "k", "m", 1, 2, 0)
    (merged,) = pu.aggregate([r, r])
    assert (merged.tokens_in, merged.tokens_out) == (2, 4)


def test_ids_are_tenant_scoped_and_stable():
    r = pu.UsageRecord("openai", "completions", START, END, "p", "k", "m", 1, 1, 1)
    other = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert pu.event_id_for(TENANT, r) == pu.event_id_for(TENANT, r)
    assert pu.event_id_for(TENANT, r) != pu.event_id_for(other, r)
    assert pu.asset_id_for(TENANT, r) != pu.asset_id_for(other, r)


async def test_errors_and_logs_never_contain_the_admin_key(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    db = FakeSession()
    caplog.set_level(logging.DEBUG)
    result = await pu.pull_provider_usage(
        db,
        TENANT,
        start=START,
        end=END,
        settings=_settings(openai=OPENAI_KEY, anthropic=ANTHROPIC_KEY),
        transport=httpx.MockTransport(handler),
        sleep=_no_sleep,
    )
    assert result["providers"]["openai"]["status"] == "error"
    assert result["providers"]["anthropic"]["status"] == "error"
    blob = repr(result) + caplog.text + repr(_settings(openai=OPENAI_KEY))
    assert OPENAI_KEY not in blob and ANTHROPIC_KEY not in blob
    assert db.of(AIAsset) == []


async def test_gives_up_after_max_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(pu.ProviderUsageError):
        await pu.fetch_openai_usage(
            SecretStr(OPENAI_KEY),
            START,
            END,
            transport=httpx.MockTransport(handler),
            sleep=_no_sleep,
        )


def test_admin_keys_come_from_env(monkeypatch):
    monkeypatch.setenv(
        "PS_PROVIDER_ADMIN_KEYS",
        f'{{"{TENANT}": {{"openai": "{OPENAI_KEY}", "bogus": "x", "anthropic": ""}}}}',
    )
    keys = pu.admin_keys_for_tenant(TENANT)
    assert list(keys) == ["openai"]
    assert keys["openai"].get_secret_value() == OPENAI_KEY
    assert pu.admin_keys_for_tenant(uuid.uuid4()) == {}


def test_malformed_env_does_not_echo_the_key(monkeypatch):
    monkeypatch.setenv(
        "PS_PROVIDER_ADMIN_KEYS", f'{{"{TENANT}": "{OPENAI_KEY}"}}'
    )
    with pytest.raises(Exception) as exc_info:
        pu.admin_keys_for_tenant(TENANT)
    assert OPENAI_KEY not in str(exc_info.value)


PULL_URL = "/api/v1/admin/provider-usage/pull"


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
    "auth",
    [None, "Bearer ps-test-key-12345", "Bearer wrong-token", "op-token"],
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
