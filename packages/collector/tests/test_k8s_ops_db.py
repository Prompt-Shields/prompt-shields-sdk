"""/readyz and ingest metrics against a real database."""

from collector import metrics
from collector.app import app
from httpx import ASGITransport, AsyncClient

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _ingested(outcome: str) -> float:
    return (
        metrics.registry.get_sample_value(
            "ps_collector_ingest_events_total",
            {"endpoint": "events", "outcome": outcome},
        )
        or 0.0
    )


def _assets_created() -> float:
    return (
        metrics.registry.get_sample_value(
            "ps_collector_assets_created_total", {"endpoint": "events"}
        )
        or 0.0
    )


async def test_readyz_200_when_the_database_answers(seeded_db):
    async with _client() as c:
        resp = await c.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


async def test_ingest_counts_accepted_events_and_new_assets(seeded_db):
    accepted, created = _ingested("accepted"), _assets_created()
    events = [
        {"vendor": "openai", "model": "gpt-4o", "source": "sdk", "tokens_in": 3},
        {"vendor": "openai", "model": "gpt-4o", "source": "sdk", "tokens_in": 4},
        {"vendor": "anthropic", "model": "claude-x", "source": "sdk"},
    ]
    async with _client() as c:
        resp = await c.post("/ingest/events", headers=HEADERS, json={"events": events})
    assert resp.status_code == 200, resp.text
    assert _ingested("accepted") == accepted + 3
    assert _assets_created() == created + resp.json()["assets_created"] == created + 2
