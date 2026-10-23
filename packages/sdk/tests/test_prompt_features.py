"""Local prompt feature extraction (#45): features, determinism, cost, privacy."""

from __future__ import annotations

import json
import statistics
import time
from unittest.mock import MagicMock

from prompt_shields import AsyncShieldsClient, ShieldsClient
from prompt_shields.prompt_features import (
    EMBED_DIM,
    FEATURE_SCHEMA_VERSION,
    PromptFeatures,
    detect_language,
    embed_text,
    extract_prompt_features,
)
from prompt_shields.route_policy import RouteRequest, _is_complex

SECRET = "Quarterly revenue for Project Nightjar was 4.2 million"
MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": f"{SECRET}. Summarise it for the board."},
]

# --- features ----------------------------------------------------------------


def test_simple_prompt_features():
    f = extract_prompt_features(MESSAGES, use_case="board-summary")
    assert f.to_dict() == {
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "approx_input_tokens": f.approx_input_tokens,
        "message_count": 2,
        "has_code": False,
        "has_json_schema": False,
        "language": "en",
        "use_case": "board-summary",
        "complexity": "simple",
    }
    assert f.approx_input_tokens > 0
    assert f.embedding is None  # only on request


def test_code_schema_and_long_prompts_are_complex():
    code = [{"role": "user", "content": "Fix this:\n```py\nx = 1\n```"}]
    schema = [{"role": "user", "content": "Answer as strict JSON"}]
    long = [{"role": "user", "content": "word " * 2000}]
    assert extract_prompt_features(code).has_code
    assert extract_prompt_features(schema).has_json_schema
    for msgs in (code, schema, long):
        assert extract_prompt_features(msgs).complexity == "complex"


def test_complexity_matches_router_heuristic_for_string_content():
    cases = [
        MESSAGES,
        [{"role": "user", "content": "```js\n1\n```"}],
        [{"role": "user", "content": "use response_format please"}],
        [{"role": "user", "content": "a" * 8001}],
        [{"role": "user", "content": "a" * 8000}],
    ]
    for msgs in cases:
        f = extract_prompt_features(msgs)
        req = RouteRequest(messages=msgs, approx_input_tokens=f.approx_input_tokens)
        assert (f.complexity == "complex") == _is_complex(req)


def test_content_parts_are_read_and_odd_input_does_not_raise():
    parts = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "```sql\nselect 1\n```"},
                {"type": "image_url", "image_url": {"url": "data:..."}},
            ],
        }
    ]
    assert extract_prompt_features(parts).has_code
    for odd in (None, "text", [None, 3, {"content": None}], {}):
        f = extract_prompt_features(odd, embed=True)
        assert f.approx_input_tokens == 0 and f.language == "und"


def test_language_labels():
    assert detect_language("Where is the station? It is not far from the hotel") == "en"
    assert detect_language("Wo ist der Bahnhof? Es ist nicht weit von hier") == "de"
    assert detect_language("¿Dónde está la estación? Es para los estudiantes") == "es"
    assert detect_language("Où est la gare? Elle est dans le centre pour les") == "fr"
    assert detect_language("東京駅はどこですか") == "ja"
    assert detect_language("火车站在哪里") == "zh"
    assert detect_language("기차역이 어디예요") == "ko"
    assert detect_language("Где вокзал") == "und-Cyrl"
    assert detect_language("xyzzy plugh") == "und-Latn"
    assert detect_language("12345 !!") == "und"


# --- embedding -----------------------------------------------------------------


def test_embedding_is_normalised_deterministic_and_local_only():
    f = extract_prompt_features(MESSAGES, embed=True)
    assert f.embedding is not None and len(f.embedding) == EMBED_DIM
    assert abs(sum(v * v for v in f.embedding) - 1.0) < 1e-4
    assert extract_prompt_features(MESSAGES, embed=True) == f
    assert "embedding" not in f.to_dict()
    assert "embedding" not in repr(f)
    assert embed_text("") == (0.0,) * EMBED_DIM


def test_embedding_is_pinned_across_platforms():
    # CRC-32 bucketing: these values change only with FEATURE_SCHEMA_VERSION.
    def nonzero(text):
        return {i: v for i, v in enumerate(embed_text(text)) if v}

    assert nonzero("hello world") == {3: -0.707107, 6: -0.707107}
    assert nonzero("HELLO   world!") == nonzero("hello world")


def test_similar_prompts_embed_closer_than_unrelated_ones():
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True))

    a = embed_text("translate this paragraph from english into german please")
    b = embed_text("please translate this paragraph into german from english now")
    c = embed_text("compute the eigenvalues of a sparse symmetric matrix")
    assert cos(a, b) > cos(a, c)


# --- privacy -------------------------------------------------------------------


def test_exported_features_hold_no_prompt_text():
    blob = json.dumps(extract_prompt_features(MESSAGES, use_case="u").to_dict())
    for word in ("Quarterly", "revenue", "Nightjar", "4.2", "board", "helpful"):
        assert word not in blob
    # Only counts, booleans and short labels: nothing hash-shaped.
    for v in extract_prompt_features(MESSAGES).to_dict().values():
        assert v is None or isinstance(v, (bool, int)) or len(str(v)) <= 32


def _response():
    resp = MagicMock()
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 20
    resp.model = "gpt-4o"
    resp.choices = []
    return resp


def _sync_client(**kwargs):
    client = ShieldsClient(
        api_key="sk-test", ps_api_key="ps-test", use_case="board-summary", **kwargs
    )
    events: list[dict] = []
    client._telemetry.enqueue = events.append
    client._telemetry.flush_sync = lambda: None
    client._upstream = MagicMock()
    client._upstream.chat.completions.create.return_value = _response()
    return client, events


def _strip_latency(event: dict) -> dict:
    return {k: v for k, v in event.items() if k != "latency_ms"}


def test_collector_event_is_unchanged_by_feature_extraction():
    """Features stay in the process: the collector gets the same event with or
    without ``on_prompt_features``, and none of the features' values."""
    seen: list[PromptFeatures] = []
    with_hook, events_with = _sync_client(on_prompt_features=seen.append)
    without, events_without = _sync_client()
    with_hook.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    without.chat.completions.create(model="gpt-4o", messages=MESSAGES)

    assert len(seen) == 1 and seen[0].embedding is not None
    assert seen[0].use_case == "board-summary"
    assert len(events_with) == len(events_without) == 1
    event = events_with[0]
    assert _strip_latency(event) == _strip_latency(events_without[0])

    feature_keys = set(seen[0].to_dict()) | {"embedding", "features", "prompt_features"}
    assert not feature_keys & (set(event) - {"use_case_name"})
    blob = json.dumps(event, default=str)
    assert SECRET not in blob and "Nightjar" not in blob
    assert FEATURE_SCHEMA_VERSION not in blob
    assert not any(
        isinstance(v, (list, tuple)) and len(v) == EMBED_DIM for v in event.values()
    )


def test_hook_errors_do_not_break_the_call():
    def boom(_):
        raise RuntimeError("learner down")

    client, events = _sync_client(on_prompt_features=boom)
    assert client.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert len(events) == 1


async def test_async_client_hook_and_event_unchanged():
    seen: list[PromptFeatures] = []

    def make(**kwargs):
        client = AsyncShieldsClient(api_key="sk-test", ps_api_key="ps-test", **kwargs)
        events: list[dict] = []
        client._telemetry.enqueue = events.append

        async def noop():
            return None

        async def upstream(**_):
            return _response()

        client._telemetry.flush = noop
        client.chat.completions._call_upstream = upstream
        return client, events

    a, ev_a = make(on_prompt_features=seen.append)
    b, ev_b = make()
    await a.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    await b.chat.completions.create(model="gpt-4o", messages=MESSAGES)
    assert len(seen) == 1
    assert _strip_latency(ev_a[0]) == _strip_latency(ev_b[0])


# --- cost ------------------------------------------------------------------------


def test_per_call_overhead_p50_under_10ms():
    """PRD target: under 10 ms p50, embedding included, on a ~4k-token prompt.
    About 2 ms on the dev container; the bound leaves room for slow CI runners."""
    prose = "Please review the attached contract and list the risky clauses. "
    msgs = [
        {"role": "system", "content": "You are a careful legal assistant."},
        {"role": "user", "content": prose * 250},
    ]
    for _ in range(5):
        extract_prompt_features(msgs, embed=True)
    samples = []
    for _ in range(101):
        t0 = time.perf_counter()
        extract_prompt_features(msgs, embed=True)
        samples.append(time.perf_counter() - t0)
    assert statistics.median(samples) < 0.010


def test_teardown_uses_the_shared_extractor():
    from prompt_shields.teardown import prompt_features

    assert prompt_features(MESSAGES, "faq") == extract_prompt_features(
        MESSAGES, use_case="faq"
    ).to_dict()
