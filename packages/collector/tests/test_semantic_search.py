"""Tests for pgvector semantic search and the pluggable embeddings backends.

No test downloads a model or makes an outbound call: the local backend runs
against a stand-in for fastembed, and the rest use a tiny deterministic
bag-of-words embedder.
"""

import hashlib
import math
import re
import sys
import types

import httpx
import pytest
from collector import embeddings
from collector.config import settings
from collector.embeddings import (
    EMBEDDING_DIM,
    LocalEmbedder,
    OpenAIEmbedder,
    build_asset_text,
    build_embedder,
    fit_to_column,
    get_embedding,
    mock_embedding,
    reset_embedder,
    set_embedder,
)
from httpx import ASGITransport, AsyncClient

HEADERS = {"Authorization": "Bearer ps-test-key-12345"}
TINY_DIM = 64


def test_build_asset_text():
    text = build_asset_text(
        vendor="openai",
        model="gpt-4o",
        use_case_name="screening",
        business_unit="HR",
        data_classification="confidential",
    )
    assert "openai" in text
    assert "gpt-4o" in text
    assert "screening" in text
    assert "HR" in text
    assert "confidential" in text


def test_build_asset_text_minimal():
    text = build_asset_text(
        vendor="openai",
        model=None,
        use_case_name=None,
        business_unit=None,
        data_classification=None,
    )
    assert text == "AI vendor: openai"


def test_mock_embedding_dimensions():
    emb = mock_embedding("test query")
    assert len(emb) == EMBEDDING_DIM
    assert all(isinstance(v, float) for v in emb)


def test_mock_embedding_deterministic():
    emb1 = mock_embedding("same input")
    emb2 = mock_embedding("same input")
    assert emb1 == emb2


def test_mock_embedding_different_inputs():
    emb1 = mock_embedding("input A")
    emb2 = mock_embedding("input B")
    assert emb1 != emb2


# --- Pluggable embeddings backends (#86) ---


def tiny_vector(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: shared words => higher cosine."""
    vec = [0.0] * TINY_DIM
    for word in re.findall(r"\w+", text.lower()):
        vec[int(hashlib.sha256(word.encode()).hexdigest(), 16) % TINY_DIM] += 1.0
    return vec


class TinyEmbedder:
    name = "tiny"

    def __init__(self):
        self.calls = []

    async def embed(self, text):
        self.calls.append(text)
        return tiny_vector(text)


class _FakeArray:
    def __init__(self, values):
        self._values = values

    def tolist(self):
        return list(self._values)


_fake_models = []


class _FakeTextEmbedding:
    """Stands in for fastembed.TextEmbedding; records how it was built."""

    def __init__(self, model_name, cache_dir=None):
        self.model_name = model_name
        self.cache_dir = cache_dir
        _fake_models.append(self)

    def embed(self, texts):
        for t in texts:
            yield _FakeArray(tiny_vector(t))


@pytest.fixture(autouse=True)
def _fresh_embedder(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "embeddings_backend", "auto")
    monkeypatch.setattr(settings, "embeddings_model", None)
    reset_embedder()
    yield
    reset_embedder()


@pytest.fixture
def fake_fastembed(monkeypatch):
    _fake_models.clear()
    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        types.SimpleNamespace(TextEmbedding=_FakeTextEmbedding),
    )


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("outbound HTTP attempted")

    monkeypatch.setattr(embeddings.httpx, "AsyncClient", refuse)


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_fit_to_column_pads_and_preserves_cosine():
    a, b = tiny_vector("hr screening"), tiny_vector("hr contract review")
    pa, pb = fit_to_column(a), fit_to_column(b)
    assert len(pa) == EMBEDDING_DIM
    assert pa[TINY_DIM:] == [0.0] * (EMBEDDING_DIM - TINY_DIM)
    assert _cosine(pa, pb) == pytest.approx(_cosine(a, b))


def test_fit_to_column_rejects_wider_vectors():
    with pytest.raises(ValueError, match="1536"):
        fit_to_column([0.1] * (EMBEDDING_DIM + 1))


def test_auto_without_key_disables_embeddings():
    assert build_embedder() is None


def test_auto_with_key_uses_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    embedder = build_embedder()
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.url == "https://api.openai.com/v1/embeddings"


def test_none_ignores_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "embeddings_backend", "none")
    assert build_embedder() is None


def test_openai_backend_requires_key(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_backend", "openai")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_embedder()


def test_unknown_backend_rejected(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_backend", "bogus")
    with pytest.raises(ValueError, match="PS_EMBEDDINGS_BACKEND"):
        build_embedder()


def test_local_backend_without_fastembed_explains_extra(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_backend", "local")
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(RuntimeError, match="local-embeddings"):
        build_embedder()


async def test_local_backend_embeds_without_network(
    monkeypatch, fake_fastembed, no_network
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-be-used")
    monkeypatch.setattr(settings, "embeddings_backend", "local")
    monkeypatch.setattr(settings, "embeddings_cache_dir", "/models")
    embedder = build_embedder()
    assert isinstance(embedder, LocalEmbedder)
    assert embedder.name == "local:BAAI/bge-small-en-v1.5"

    set_embedder(embedder)
    text = "AI vendor: openai, business unit: HR"
    vec = await get_embedding(text)
    assert len(vec) == EMBEDDING_DIM
    assert vec[:TINY_DIM] == tiny_vector(text)
    # The model is loaded lazily, once, from the configured cache dir.
    await get_embedding("again")
    assert len(_fake_models) == 1
    assert _fake_models[0].cache_dir == "/models"


async def test_local_backend_backs_off_after_failed_model_load(monkeypatch):
    attempts = []

    class Unreachable:
        def __init__(self, model_name, cache_dir=None):
            attempts.append(model_name)
            raise OSError("model hub unreachable")

    monkeypatch.setitem(
        sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=Unreachable)
    )
    set_embedder(LocalEmbedder(model="m"))
    assert await get_embedding("a") is None
    assert await get_embedding("b") is None
    assert attempts == ["m"]


async def test_openai_backend_honours_base_url_and_model(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"data": [{"embedding": [0.5] * 8}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        embeddings.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "embeddings_backend", "openai")
    monkeypatch.setattr(settings, "embeddings_model", "eu-embed-small")
    monkeypatch.setattr(
        settings, "embeddings_openai_base_url", "https://eu.example.test/v1/"
    )

    vec = await get_embedding("hello")
    assert seen["url"] == "https://eu.example.test/v1/embeddings"
    assert b'"eu-embed-small"' in seen["body"]
    assert vec[:8] == [0.5] * 8
    assert len(vec) == EMBEDDING_DIM


async def test_disabled_backend_makes_no_call(monkeypatch, no_network):
    monkeypatch.setattr(settings, "embeddings_backend", "none")
    assert await get_embedding("anything") is None


async def test_failed_backend_returns_none():
    class Broken:
        name = "broken"

        async def embed(self, text):
            return None

    set_embedder(Broken())
    assert await get_embedding("x") is None


# --- /search end to end (needs the test Postgres, like test_registry.py) ---


async def _seed(client):
    events = [
        {
            "vendor": "openai",
            "model": "gpt-4o",
            "use_case_name": "screening",
            "business_unit": "HR",
            "source": "sdk",
        },
        {
            "vendor": "anthropic",
            "model": "claude-sonnet-4",
            "use_case_name": "contract-review",
            "business_unit": "Legal",
            "source": "gateway",
        },
    ]
    resp = await client.post("/ingest/events", headers=HEADERS, json={"events": events})
    assert resp.status_code < 300


async def test_search_semantic_with_local_embedder(seeded_db):
    from collector.app import app

    tiny = TinyEmbedder()
    set_embedder(tiny)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _seed(client)
        resp = await client.get(
            "/api/v1/registry/search",
            params={"q": "legal contract review"},
            headers=HEADERS,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["mode"] == "semantic"
    assert body["data"][0]["business_unit"] == "Legal"
    assert body["data"][0]["similarity"] > body["data"][1]["similarity"]
    # Two assets embedded at ingest, plus the query.
    assert len(tiny.calls) == 3


async def test_search_falls_back_to_keywords_when_disabled(
    seeded_db, monkeypatch, no_network
):
    from collector.app import app

    monkeypatch.setattr(settings, "embeddings_backend", "none")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _seed(client)
        resp = await client.get(
            "/api/v1/registry/search",
            params={"q": "HR screening tools"},
            headers=HEADERS,
        )
        miss = await client.get(
            "/api/v1/registry/search", params={"q": "zzz_nothing%"}, headers=HEADERS
        )

    body = resp.json()
    assert body["meta"]["mode"] == "keyword"
    assert [a["business_unit"] for a in body["data"]] == ["HR"]
    assert body["data"][0]["score"] == pytest.approx(2 / 3, abs=1e-4)
    assert miss.json()["data"] == []


async def test_search_is_audited_in_both_modes(seeded_db, monkeypatch, no_network):
    from collector.app import app
    from db.models import PartnerAuditLog
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        set_embedder(TinyEmbedder())
        await _seed(client)
        semantic = await client.get(
            "/api/v1/registry/search", params={"q": "legal contract"}, headers=HEADERS
        )
        set_embedder(None)
        keyword = await client.get(
            "/api/v1/registry/search", params={"q": "HR screening"}, headers=HEADERS
        )

    assert semantic.json()["meta"]["mode"] == "semantic"
    assert keyword.json()["meta"]["mode"] == "keyword"
    async with AsyncSession(seeded_db) as session:
        rows = (
            await session.execute(
                select(PartnerAuditLog)
                .where(PartnerAuditLog.endpoint == "/api/v1/registry/search")
                .order_by(PartnerAuditLog.timestamp)
            )
        ).scalars().all()

    assert [r.response_count for r in rows] == [
        len(semantic.json()["data"]),
        len(keyword.json()["data"]),
    ]
    assert [str(i) for i in rows[1].asset_ids] == [a["id"] for a in keyword.json()["data"]]
    # The search text is stored hashed, never as plain text.
    assert all(r.query_params["q"].startswith("sha256:") for r in rows)


async def test_reembed_rebuilds_then_clears(seeded_db):
    from collector.app import app
    from collector.reembed import reembed
    from db.models import AIAsset
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await _seed(client)  # embeddings off: assets stored without vectors

    factory = async_sessionmaker(seeded_db, class_=AsyncSession)
    set_embedder(TinyEmbedder())
    assert await reembed(factory) == (2, 0)
    async with factory() as session:
        vectors = (await session.execute(select(AIAsset.embedding))).scalars().all()
    assert all(v is not None and len(v) == EMBEDDING_DIM for v in vectors)

    set_embedder(None)
    assert await reembed(factory) == (0, 0)
    async with factory() as session:
        vectors = (await session.execute(select(AIAsset.embedding))).scalars().all()
    assert vectors == [None, None]
