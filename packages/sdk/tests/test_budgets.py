"""Spend budgets (#29): budget.v1, the stores, and enforcement in
ShieldsRouter and the plain clients.

The shared vectors in ``schemas/budget/vectors/`` are the contract with the
gateway (#28). Redis is faked with fakeredis (its Lua support runs the real
increment script); nothing needs a Redis server.
"""

from __future__ import annotations

import copy
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from prompt_shields import (
    AsyncShieldsClient,
    AsyncShieldsRouter,
    BudgetEnforcer,
    BudgetError,
    BudgetExceeded,
    InProcessBudgetStore,
    RedisBudgetStore,
    RouteHint,
    RouterError,
    ShieldsClient,
    ShieldsRouter,
)
from prompt_shields.budgets import (
    Budget,
    BudgetStatus,
    budget_state,
    counter_key,
    counter_ttl,
    decide,
    load_budget_policy,
    window_period,
)
from prompt_shields.failover import NoCompliantCandidate

from tests import test_router_failover as fo
from tests.test_router import (
    HI,
    NORDIC,
    NORDIC_PROVIDERS,
    _FakeUpstream,
    _policy,
    _wire,
)
from tests.test_router_failover import ProviderError

SCHEMA_DIR = Path(__file__).resolve().parents[3] / "schemas" / "budget"
KEY_VECTORS = json.loads((SCHEMA_DIR / "vectors" / "keys.json").read_text())
DECISION_VECTORS = json.loads((SCHEMA_DIR / "vectors" / "decisions.json").read_text())
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
DETERMINISTIC = _policy("deterministic")


def _doc(*budgets, **extra) -> dict:
    return {"schema_version": "budget.v1", "budgets": list(budgets), **extra}


def _budget(**kw) -> dict:
    out = {"id": "b", "scope": "global", "window": "day", "limit_usd": 1.0,
           "mode": "block"}
    out.update(kw)
    return out


def _enforcer(*budgets, store=None, **extra) -> BudgetEnforcer:
    return BudgetEnforcer(_doc(*budgets, **extra), store, clock=lambda: NOW)


def _spend(enforcer: BudgetEnforcer, amount: float, **attrs) -> None:
    enforcer.commit(enforcer.check(attrs), amount)


def _fakeredis():
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")
    return fakeredis


class _BrokenStore:
    blocking = False

    def get_many(self, keys):
        raise ConnectionError("down")

    def add(self, key, amount, ttl_seconds):
        raise ConnectionError("down")


# --- shared vectors -------------------------------------------------------------


@pytest.mark.parametrize("vector", KEY_VECTORS["vectors"], ids=lambda v: v["id"])
def test_key_vector(vector):
    i = vector["input"]
    now = datetime.fromisoformat(i["now"])
    period, _ = window_period(i["window"], now)
    assert counter_key(i["namespace"], i["scope"], i["subject"], i["window"],
                       period) == vector["expected"]["key"]
    assert counter_ttl(i["window"], now) == vector["expected"]["ttl_seconds"]


@pytest.mark.parametrize("vector", DECISION_VECTORS["vectors"], ids=lambda v: v["id"])
def test_decision_vector(vector):
    doc = copy.deepcopy(DECISION_VECTORS["budgets"])
    if "on_store_error" in vector:
        doc["on_store_error"] = vector["on_store_error"]
    policy = load_budget_policy(doc)
    matched = [b for b in policy.budgets if b.subject_for(vector["attributes"])]
    statuses = [
        BudgetStatus(b, "k", 1, vector["spent"][b.id],
                     budget_state(b, vector["spent"][b.id]))
        for b in matched
    ]
    check = decide(statuses, policy.on_store_error)
    expected = vector["expected"]
    actual = {
        "matched": [b.id for b in matched],
        "state": check.state,
        "budget_id": check.budget_id,
        "action": check.action,
        "ceiling_usd": check.ceiling_usd,
        "downgrade_model": check.downgrade_model,
    }
    assert {k: actual[k] for k in expected} == expected


def test_decision_vectors_run_through_the_enforcer():
    """The same decisions end to end: keys, store reads and matching."""
    doc = DECISION_VECTORS["budgets"]
    for vector in DECISION_VECTORS["vectors"]:
        if any(v is None for v in vector["spent"].values()):
            continue
        enforcer = BudgetEnforcer(doc, clock=lambda: NOW)
        # Budgets on the same scope, subject and window share a counter.
        written: dict[str, float] = {}
        for s in enforcer.check(vector["attributes"]).statuses:
            amount = vector["spent"][s.budget.id]
            assert written.setdefault(s.key, amount) == amount, vector["id"]
        for key, amount in written.items():
            enforcer.store.add(key, amount, 60)
        check = enforcer.check(vector["attributes"])
        assert check.state == vector["expected"]["state"], vector["id"]
        assert check.action == vector["expected"]["action"], vector["id"]


def test_documents_match_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((SCHEMA_DIR / "budget.v1.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(DECISION_VECTORS["budgets"], schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_doc(_budget(mode="throttle")), schema)


# --- document -------------------------------------------------------------------


@pytest.mark.parametrize(
    "doc,match",
    [
        ({"budgets": []}, "schema_version"),
        (_doc(_budget(mode="throttle")), "mode"),
        (_doc(_budget(scope="team", subject="x")), "scope"),
        (_doc(_budget(subject="x")), "no subject"),
        (_doc(_budget(scope="use_case")), "needs a subject"),
        (_doc(_budget(limit_usd=-1)), "limit_usd"),
        (_doc(_budget(limit_usd=True)), "limit_usd"),
        (_doc(_budget(warn_at=0)), "warn_at"),
        (_doc(_budget(window="year")), "window"),
        (_doc(_budget(), _budget()), "unique"),
        (_doc(_budget(downgrade_model="m")), "downgrade_model"),
        (_doc(_budget(extra=1)), "unknown field"),
        (_doc(_budget(), on_store_error="maybe"), "on_store_error"),
        (_doc(_budget(), namespace="Acme"), "namespace"),
    ],
)
def test_invalid_documents_are_rejected(doc, match):
    with pytest.raises(BudgetError, match=match):
        load_budget_policy(doc)


def test_document_from_file(tmp_path):
    path = tmp_path / "budgets.json"
    path.write_text(json.dumps(_doc(_budget())))
    assert load_budget_policy(path).budgets[0].id == "b"


def test_state_thresholds():
    b = Budget("b", "global", "day", 10.0, "warn", warn_at=0.5)
    assert [budget_state(b, s) for s in (0, 4.99, 5, 9.99, 10, None)] == [
        "ok", "ok", "warn", "warn", "exceeded", "unknown",
    ]


# --- stores ---------------------------------------------------------------------


def test_in_process_store_expires_counters():
    now = [1000.0]
    store = InProcessBudgetStore(clock=lambda: now[0])
    assert store.add("k", 1.5, 10) == 1.5
    assert store.add("k", 1.0, 999) == 2.5  # TTL kept from the first write
    assert store.get_many(["k", "missing"]) == [2.5, 0.0]
    now[0] = 1010.0
    assert store.get_many(["k"]) == [0.0]
    assert store.add("k", 1.0, 10) == 1.0


def test_redis_store_atomic_increment_and_ttl():
    fakeredis = _fakeredis()
    client = fakeredis.FakeRedis()
    store = RedisBudgetStore(client)
    assert store.get_many([]) == []
    assert store.add("ps:budget:v1:k", 0.25, 100) == pytest.approx(0.25)
    assert client.ttl("ps:budget:v1:k") == 100
    assert store.add("ps:budget:v1:k", 1e-05, 5000) == pytest.approx(0.25001)
    assert client.ttl("ps:budget:v1:k") == 100  # never extended
    assert store.get_many(["ps:budget:v1:k", "nope"]) == [pytest.approx(0.25001), 0.0]

    threads = [threading.Thread(target=lambda: [store.add("c", 0.5, 60)
                                                for _ in range(50)])
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_many(["c"]) == [pytest.approx(100.0)]


def test_redis_store_without_redis_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "redis", None)
    with pytest.raises(BudgetError, match="optimizer-redis"):
        RedisBudgetStore()


def test_enforcer_records_cost_once_per_counter():
    enforcer = _enforcer(
        _budget(id="a", scope="use_case", subject="bot", mode="warn"),
        _budget(id="b", scope="use_case", subject="*", mode="block", limit_usd=5),
    )
    check = enforcer.check({"use_case": "bot"})
    assert len(check.statuses) == 2 and len(check.counter_keys()) == 1
    enforcer.commit(check, 0.5)
    enforcer.commit(check, None)
    enforcer.commit(check, -1)
    assert enforcer.check({"use_case": "bot"}).statuses[0].spent_usd == 0.5
    assert enforcer.check({"use_case": "other"}).statuses[0].spent_usd == 0.0
    assert enforcer.check({}).state is None


def test_store_errors_fail_open_by_default_and_closed_when_configured():
    open_ = _enforcer(_budget(), store=_BrokenStore())
    check = open_.check({})
    assert (check.state, check.action) == ("unknown", "none")
    open_.raise_if_blocked(check)
    open_.commit(check, 1.0)  # a failed write never raises

    closed = _enforcer(_budget(), store=_BrokenStore(), on_store_error="closed")
    with pytest.raises(BudgetExceeded, match="could not be read") as err:
        closed.raise_if_blocked(closed.check({}))
    assert err.value.state == "unknown"


def test_windows_roll_over():
    clock = [datetime(2026, 11, 6, 23, 59, tzinfo=UTC)]
    enforcer = BudgetEnforcer(_doc(_budget(limit_usd=1)), clock=lambda: clock[0])
    _spend(enforcer, 1.0)
    assert enforcer.check({}).state == "exceeded"
    clock[0] = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
    assert enforcer.check({}).state == "ok"


# --- ShieldsRouter --------------------------------------------------------------


def _det_router(enforcer=None, cls=ShieldsRouter, **kwargs):
    return cls(
        ps_api_key="ps", providers={"openai": {"api_key": "sk"}},
        policy=DETERMINISTIC, budgets=enforcer, **kwargs,
    )


def test_router_downgrade_clamps_to_remaining_budget():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=1.0))
    router = _det_router(enforcer)
    events, upstreams = _wire(router)
    natural = router.route(HI, route=RouteHint(quality="critical"), max_tokens=100)
    assert natural.model == "big"
    _spend(enforcer, 1.0 - 0.002)  # leaves room for "mid" (0.0010025), not "big"

    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"), max_tokens=100,
    )
    assert upstreams["openai"].calls[0]["model"] == "mid"
    assert resp.ps.route.reason == "quality:critical+budget-clamp+budget-downgrade"
    assert (resp.ps.budget.state, resp.ps.budget.action) == ("warn", "downgraded")
    (event,) = events
    assert event["budget_state"] == "warn"
    assert event["budget_id"] == "b"
    assert event["budget_action"] == "downgraded"
    assert event["route_reason"].endswith("+budget-downgrade")
    # The call's cost was recorded.
    spent = enforcer.check({}).statuses[0].spent_usd
    assert spent == pytest.approx(1.0 - 0.002 + event["cost"])


def test_router_downgrade_when_exhausted_serves_cheapest():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=0.5))
    router = _det_router(enforcer)
    events, upstreams = _wire(router)
    _spend(enforcer, 0.75)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(model_group="frontier"), max_tokens=100,
    )
    assert upstreams["openai"].calls[0]["model"] == "mini"
    assert resp.ps.route.reason == "override+budget-unsatisfiable+budget-downgrade"
    assert events[0]["budget_state"] == "exceeded"


def test_router_downgrade_noop_when_call_fits():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=10))
    router = _det_router(enforcer)
    events, _ = _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"), max_tokens=100,
    )
    assert resp.ps.route.model == "big"
    assert (events[0]["budget_state"], events[0]["budget_action"]) == ("ok", "none")


def test_router_downgrade_never_leaves_the_data_policy():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=0))
    router = ShieldsRouter(ps_api_key="ps", providers=NORDIC_PROVIDERS,
                           policy=copy.deepcopy(NORDIC), budgets=enforcer)
    _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"),
        data_classification="confidential",
    )
    # Cheapest *compliant* candidate (EEA, ZDR, no CLOUD Act), not gpt-4o-mini.
    assert resp.ps.route.jurisdiction == "EEA"
    assert resp.ps.route.model == "scaleway-small"
    assert resp.ps.budget.action == "downgraded"


def test_router_block_refuses_and_sends_nothing():
    enforcer = _enforcer(_budget(id="org", mode="block", limit_usd=1))
    router = _det_router(enforcer)
    events, upstreams = _wire(router)
    router.chat.completions.create(messages=HI, model="mini", max_tokens=10)
    _spend(enforcer, 1.0)
    with pytest.raises(BudgetExceeded, match="'org' is exhausted") as err:
        router.chat.completions.create(messages=HI, model="mini", max_tokens=10)
    assert err.value.budget_id == "org" and err.value.limit_usd == 1
    with pytest.raises(BudgetExceeded):
        router.route(HI)
    assert len(upstreams["openai"].calls) == 1 and len(events) == 1


def test_router_warn_never_changes_the_call():
    enforcer = _enforcer(_budget(mode="warn", limit_usd=0))
    router = _det_router(enforcer)
    events, _ = _wire(router)
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"), max_tokens=100,
    )
    assert resp.ps.route.model == "big"
    assert (events[0]["budget_state"], events[0]["budget_action"]) == ("exceeded", "none")


def test_router_budgets_match_attribution():
    enforcer = _enforcer(
        _budget(id="support", scope="use_case", subject="support", mode="block",
                limit_usd=0),
        _budget(id="bu", scope="business_unit", subject="finance", mode="warn"),
    )
    router = _det_router(enforcer, business_unit="finance")
    events, _ = _wire(router)
    router.chat.completions.create(messages=HI, use_case="chat")
    assert events[0]["budget_id"] == "bu"
    with pytest.raises(BudgetExceeded):
        router.chat.completions.create(messages=HI, use_case="support")


def test_router_without_matching_budget_has_no_budget_fields():
    router = _det_router(_enforcer(_budget(scope="owner", subject="x")))
    events, _ = _wire(router)
    resp = router.chat.completions.create(messages=HI)
    assert "budget_state" not in events[0] and resp.ps.budget is None


def test_router_budget_document_and_store_kwargs():
    store = InProcessBudgetStore()
    router = _det_router(_doc(_budget(mode="warn")), budget_store=store)
    _wire(router)
    router.chat.completions.create(messages=HI, model="mini", max_tokens=10)
    assert router.budgets.store is store
    assert router.budgets.check({}).statuses[0].spent_usd > 0
    with pytest.raises(RouterError, match="invalid budgets"):
        _det_router({"budgets": []})
    with pytest.raises(RouterError, match="gateway"):
        ShieldsRouter(ps_api_key="ps", mode="gateway", gateway_url="https://gw/v1",
                      budgets=_doc(_budget()))


async def test_async_routers_share_a_redis_budget():
    """Two routers (as two processes) on one Redis see each other's spend."""
    fakeredis = _fakeredis()
    server = fakeredis.FakeServer()
    doc = _doc(_budget(id="shared", mode="block", limit_usd=0.0001))
    a = _det_router(BudgetEnforcer(doc, RedisBudgetStore(fakeredis.FakeRedis(server=server))),
                    cls=AsyncShieldsRouter)
    b = _det_router(BudgetEnforcer(doc, RedisBudgetStore(fakeredis.FakeRedis(server=server))),
                    cls=AsyncShieldsRouter)
    events, _ = _wire(a, is_async=True)
    _wire(b, is_async=True)
    await a.chat.completions.create(messages=HI, model="big", max_tokens=100)
    assert events[0]["budget_state"] == "ok"
    with pytest.raises(BudgetExceeded):
        await b.chat.completions.create(messages=HI, model="mini", max_tokens=10)


async def test_async_router_store_down_fails_open():
    router = _det_router(_enforcer(_budget(), store=_BrokenStore()),
                         cls=AsyncShieldsRouter)
    events, _ = _wire(router, is_async=True)
    await router.chat.completions.create(messages=HI)
    assert events[0]["budget_state"] == "unknown"


# --- plain clients --------------------------------------------------------------


def _client(enforcer, cls=ShieldsClient, **kwargs):
    client = cls(api_key="sk", ps_api_key="ps", vendor="openai", budgets=enforcer,
                 **kwargs)
    events: list[dict] = []
    is_async = cls is AsyncShieldsClient
    client._upstream = _FakeUpstream("openai", is_async)
    client._telemetry.enqueue = events.append
    if is_async:
        async def noop():
            return None
        client._telemetry.flush = noop
    else:
        client._telemetry.flush_sync = lambda: None
    return client, events


def test_client_block_and_cost_recording():
    enforcer = _enforcer(_budget(scope="use_case", subject="bot", limit_usd=0.0001))
    client, events = _client(enforcer, use_case="bot")
    client.chat.completions.create(model="gpt-4o-mini", messages=HI)
    assert events[0]["budget_state"] == "ok"
    assert enforcer.check({"use_case": "bot"}).statuses[0].spent_usd == events[0]["cost"]
    _spend(enforcer, 1.0, use_case="bot")
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="gpt-4o-mini", messages=HI)
    assert len(client._upstream.calls) == 1


def test_client_downgrade_switches_to_downgrade_model():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=1,
                                 downgrade_model="gpt-4o-mini"))
    client, events = _client(enforcer, allow_budget_downgrade=True)
    _spend(enforcer, 1.0)
    client.chat.completions.create(model="gpt-4o", messages=HI)
    assert client._upstream.calls[0]["model"] == "gpt-4o-mini"
    assert events[0]["requested_model"] == "gpt-4o"
    assert events[0]["served_model"] == "gpt-4o-mini"
    assert (events[0]["budget_state"], events[0]["budget_action"]) == (
        "exceeded", "downgraded")


def test_client_downgrade_needs_opt_in(caplog):
    # A plain client runs no data policy: without allow_budget_downgrade the
    # downgrade_model is never sent to, the call goes unchanged and it is logged.
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=1,
                                 downgrade_model="gpt-4o-mini"))
    with caplog.at_level("WARNING", logger="prompt_shields.budgets"):
        client, events = _client(enforcer)
    assert "allow_budget_downgrade" in caplog.text
    _spend(enforcer, 1.0)
    caplog.clear()
    with caplog.at_level("WARNING", logger="prompt_shields.budgets"):
        client.chat.completions.create(model="gpt-4o", messages=HI)
    assert client._upstream.calls[0]["model"] == "gpt-4o"
    assert events[0]["requested_model"] == events[0]["served_model"] == "gpt-4o"
    assert (events[0]["budget_state"], events[0]["budget_action"]) == (
        "exceeded", "none")
    assert "not applied" in caplog.text


async def test_async_client_downgrade_opt_in():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=1,
                                 downgrade_model="gpt-4o-mini"))
    _spend(enforcer, 1.0)
    client, events = _client(enforcer, cls=AsyncShieldsClient)
    await client.chat.completions.create(model="gpt-4o", messages=HI)
    assert client._upstream.calls[0]["model"] == "gpt-4o"
    assert events[0]["budget_action"] == "none"
    client, events = _client(enforcer, cls=AsyncShieldsClient,
                             allow_budget_downgrade=True)
    await client.chat.completions.create(model="gpt-4o", messages=HI)
    assert client._upstream.calls[0]["model"] == "gpt-4o-mini"
    assert events[0]["budget_action"] == "downgraded"


def test_client_downgrade_without_model_serves_unchanged():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=0))
    client, events = _client(enforcer)
    client.chat.completions.create(model="gpt-4o", messages=HI)
    assert client._upstream.calls[0]["model"] == "gpt-4o"
    assert events[0]["budget_action"] == "none"


def test_client_without_budgets_is_unchanged():
    client, events = _client(None)
    client.chat.completions.create(model="gpt-4o", messages=HI)
    assert "budget_state" not in events[0]
    with pytest.raises(BudgetError, match="without budgets"):
        ShieldsClient(api_key="sk", ps_api_key="ps", budget_store=InProcessBudgetStore())


async def test_async_client_block():
    enforcer = _enforcer(_budget(limit_usd=1e-6))
    client, events = _client(enforcer, cls=AsyncShieldsClient)
    await client.chat.completions.create(model="gpt-4o-mini", messages=HI)
    assert events[0]["budget_state"] == "ok"
    with pytest.raises(BudgetExceeded):
        await client.chat.completions.create(model="gpt-4o-mini", messages=HI)


# --- budgets with failover (#21 + #29) -------------------------------------------


def _failover_router(enforcer, failures, cls=ShieldsRouter):
    router = fo._router(cls=cls, policy=copy.deepcopy(fo.NORDIC), budgets=enforcer)
    events, upstreams = fo._wire(router, failures, is_async=cls is AsyncShieldsRouter)
    return router, events, upstreams


def _spent(enforcer) -> float:
    return enforcer.check({}).statuses[0].spent_usd


def test_failover_downgrade_clamp_narrows_candidates_and_records_once():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=1.0))
    _spend(enforcer, 1.0 - 0.0004)  # room for the small EEA models only
    router, events, upstreams = _failover_router(
        enforcer, {"mistral-eu": [ProviderError(503)]}
    )
    resp = router.chat.completions.create(
        messages=HI, route=RouteHint(quality="critical"),
        data_classification="confidential",
    )
    # Downgraded off the frontier tier to the best model under the ceiling;
    # the fallback is the next compliant candidate under it, never
    # mistral-large-eu (same provider, above the ceiling).
    assert [c["model"] for c in upstreams["mistral-eu"].calls] == ["mistral-small-eu"]
    assert [c["model"] for c in upstreams["scaleway"].calls] == ["scaleway-small"]
    assert resp.ps.route.model == "scaleway-small"
    assert resp.ps.fallback_chain == ("server_error",)
    assert (resp.ps.budget.state, resp.ps.budget.action) == ("warn", "downgraded")
    (event,) = events
    assert (event["budget_state"], event["budget_action"]) == ("warn", "downgraded")
    assert event["fallback_chain"] == ["server_error"]
    # Only the attempt that served the call is recorded, once.
    assert _spent(enforcer) == pytest.approx(1.0 - 0.0004 + event["cost"])


def test_failover_exhausted_downgrade_never_escalates_cost():
    enforcer = _enforcer(_budget(mode="downgrade", limit_usd=0.5))
    _spend(enforcer, 0.5)
    router, events, upstreams = _failover_router(
        enforcer, {"scaleway": [ProviderError(503)]}
    )
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(
            messages=HI, route=RouteHint(quality="critical"),
            data_classification="confidential",
        )
    # The cheapest compliant model was the only candidate within the budget.
    assert {u for u, up in upstreams.items() if up.calls} == {"scaleway"}
    assert events == []
    assert _spent(enforcer) == pytest.approx(0.5)


def test_failed_attempts_do_not_consume_budget():
    enforcer = _enforcer(_budget(mode="block", limit_usd=10))
    router, events, upstreams = _failover_router(
        enforcer,
        {"scaleway": [ProviderError(503)], "mistral-eu": [ProviderError(503)]},
    )
    with pytest.raises(NoCompliantCandidate):
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert sum(len(u.calls) for u in upstreams.values()) == 2
    assert _spent(enforcer) == 0.0

    # Next call: scaleway fails again, mistral serves; one cost recorded.
    upstreams["scaleway"].failures.append(ProviderError(503))
    resp = router.chat.completions.create(messages=HI, data_classification="confidential")
    assert resp.ps.route.provider == "mistral-eu"
    (event,) = events
    assert _spent(enforcer) == pytest.approx(event["cost"])


async def test_async_failover_records_only_the_serving_attempt():
    enforcer = _enforcer(_budget(mode="block", limit_usd=10))
    router, events, _ = _failover_router(
        enforcer, {"scaleway": [ProviderError(503)]}, cls=AsyncShieldsRouter
    )
    resp = await router.chat.completions.create(
        messages=HI, data_classification="confidential"
    )
    assert resp.ps.route.provider == "mistral-eu"
    assert resp.ps.budget.state == "ok"
    (event,) = events
    assert event["budget_state"] == "ok"
    assert _spent(enforcer) == pytest.approx(event["cost"])


def test_block_budget_refuses_before_any_failover_attempt():
    enforcer = _enforcer(_budget(mode="block", limit_usd=1))
    _spend(enforcer, 1.0)
    router, events, upstreams = _failover_router(enforcer, {})
    with pytest.raises(BudgetExceeded):
        router.chat.completions.create(messages=HI, data_classification="confidential")
    assert all(not u.calls for u in upstreams.values()) and events == []
