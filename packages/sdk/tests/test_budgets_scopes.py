"""Spend budgets (#29, #347): team / project / user / service / model scopes,
token budgets, pre-call reservations and threshold notifications.

Redis is faked with fakeredis (its Lua support runs the real scripts); nothing
needs a Redis server.
"""

from __future__ import annotations

import threading

import pytest
from prompt_shields import (
    AsyncShieldsClient,
    AsyncShieldsRouter,
    BudgetEnforcer,
    BudgetError,
    BudgetExceeded,
    BudgetThreshold,
    InProcessBudgetStore,
    RedisBudgetStore,
)
from prompt_shields.budgets import (
    budget_attributes,
    counter_key,
    event_tokens,
    reservations_key,
)
from prompt_shields.feedback import hash_user_id

from tests.test_budgets import (
    HI,
    NOW,
    _budget,
    _client,
    _det_router,
    _doc,
    _enforcer,
    _fakeredis,
)
from tests.test_router import _wire

# --- documents -------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["team", "project", "user", "service", "model"])
def test_new_scopes_are_valid(scope):
    enforcer = _enforcer(_budget(scope=scope, subject="*", mode="warn"))
    assert enforcer.check({scope: "x"}).statuses[0].key.split(":")[4] == scope
    assert enforcer.check({}).state is None


@pytest.mark.parametrize(
    "budget,match",
    [
        (_budget(limit_tokens=10), "exactly one"),
        ({k: v for k, v in _budget().items() if k != "limit_usd"}, "exactly one"),
        ({**{k: v for k, v in _budget().items() if k != "limit_usd"},
          "limit_tokens": 1.5}, "limit_tokens"),
        ({**{k: v for k, v in _budget().items() if k != "limit_usd"},
          "limit_tokens": True}, "limit_tokens"),
        ({**{k: v for k, v in _budget().items() if k != "limit_usd"},
          "limit_tokens": 10, "mode": "downgrade"}, "warn' or 'block"),
    ],
)
def test_invalid_token_budgets(budget, match):
    with pytest.raises(BudgetError, match=match):
        _enforcer(budget)


def test_token_budget_matches_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    import json

    from tests.test_budgets import SCHEMA_DIR

    schema = json.loads((SCHEMA_DIR / "budget.v1.schema.json").read_text())
    tokens = {"id": "t", "scope": "team", "subject": "data", "window": "day",
              "limit_tokens": 1000, "mode": "block"}
    jsonschema.validate(_doc(tokens), schema)
    for bad in ({**tokens, "mode": "downgrade"}, {**tokens, "limit_usd": 1},
                {k: v for k, v in tokens.items() if k != "limit_tokens"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(_doc(bad), schema)


# --- attributes ------------------------------------------------------------------


def test_budget_attributes_hash_the_user_id():
    attrs = budget_attributes(
        {"business_unit": "bu", "use_case": "bot", "owner": "o",
         "calling_service": "svc"},
        {"team": "data", "project": "chat", "user_id": "alice@example.com"},
        "gpt-4o",
    )
    assert attrs == {
        "business_unit": "bu", "use_case": "bot", "owner": "o", "service": "svc",
        "team": "data", "project": "chat", "model": "gpt-4o",
        "user": hash_user_id("alice@example.com"),
    }
    assert "alice" not in attrs["user"]
    assert budget_attributes({}, {"user_id": ""})["user"] is None


def test_client_team_project_user_budgets_from_ps_metadata():
    enforcer = _enforcer(
        _budget(id="team", scope="team", subject="data", mode="warn", limit_usd=10),
        _budget(id="proj", scope="project", subject="*", mode="warn", limit_usd=10),
        _budget(id="user", scope="user", subject="*", mode="block", limit_usd=1e-9),
        _budget(id="model", scope="model", subject="gpt-4o-mini", mode="warn",
                limit_usd=10),
    )
    client, events = _client(enforcer)
    meta = {"team": "data", "project": "chat", "user_id": "alice"}
    client.chat.completions.create(model="gpt-4o-mini", messages=HI, ps_metadata=meta)
    cost = events[0]["cost"]
    assert cost > 0
    check = enforcer.check(budget_attributes({}, meta, "gpt-4o-mini"))
    assert [s.budget.id for s in check.statuses] == ["team", "proj", "user", "model"]
    assert all(s.spent_usd == pytest.approx(cost) for s in check.statuses)
    # The user's counter is keyed on the hash, never the raw id.
    user_key = check.statuses[2].key
    assert hash_user_id("alice") in user_key and "alice" not in user_key
    # Another user is not limited by alice's spend.
    with pytest.raises(BudgetExceeded) as err:
        client.chat.completions.create(model="gpt-4o-mini", messages=HI,
                                       ps_metadata=meta)
    assert err.value.budget_id == "user"
    client.chat.completions.create(model="gpt-4o-mini", messages=HI,
                                   ps_metadata={**meta, "user_id": "bob"})
    assert len(client._upstream.calls) == 2


def test_client_service_scope_from_calling_service():
    enforcer = _enforcer(_budget(scope="service", subject="billing", limit_usd=1e-9))
    client, _ = _client(enforcer, calling_service="billing")
    client.chat.completions.create(model="gpt-4o-mini", messages=HI)
    with pytest.raises(BudgetExceeded):
        client.chat.completions.create(model="gpt-4o-mini", messages=HI)


# --- token budgets ---------------------------------------------------------------


def _tokens_budget(**kw):
    out = {k: v for k, v in _budget(**kw).items() if k != "limit_usd"}
    out.setdefault("limit_tokens", 100)
    return out


def test_token_budget_counts_event_tokens_and_blocks():
    enforcer = _enforcer(_tokens_budget(scope="team", subject="data", limit_tokens=30))
    client, events = _client(enforcer)
    meta = {"team": "data"}
    client.chat.completions.create(model="gpt-4o-mini", messages=HI, ps_metadata=meta)
    used = event_tokens(events[0])
    assert used and used == events[0]["tokens_in"] + events[0]["tokens_out"]
    check = enforcer.check({"team": "data"})
    assert check.statuses[0].key.endswith(":tokens")
    assert check.statuses[0].spent_usd == used
    client.chat.completions.create(model="gpt-4o-mini", messages=HI, ps_metadata=meta)
    with pytest.raises(BudgetExceeded) as err:
        client.chat.completions.create(model="gpt-4o-mini", messages=HI,
                                       ps_metadata=meta)
    assert (err.value.unit, err.value.limit, err.value.limit_usd) == ("tokens", 30, None)
    assert err.value.spent == 2 * used and err.value.spent_usd is None


def test_usd_and_token_budgets_on_the_same_subject_keep_separate_counters():
    enforcer = _enforcer(
        _budget(id="usd", scope="team", subject="data", mode="warn", limit_usd=10),
        _tokens_budget(id="tok", scope="team", subject="data", mode="warn"),
    )
    check = enforcer.check({"team": "data"})
    assert len(check.counter_units()) == 2
    enforcer.commit(check, 0.5, 40)
    after = enforcer.check({"team": "data"})
    assert [s.spent_usd for s in after.statuses] == [0.5, 40]


# --- reservations ----------------------------------------------------------------


def test_reservation_holds_the_estimate_and_settles_to_the_actual():
    enforcer = _enforcer(_budget(mode="block", limit_usd=1.0))
    first = enforcer.reserve(enforcer.check({}), 0.6, 100)
    assert first.reservation_id and len(first.reservations) == 1
    # A concurrent call sees the held estimate.
    assert enforcer.check({}).statuses[0].spent_usd == pytest.approx(0.6)
    second = enforcer.reserve(enforcer.check({}), 0.6, 100)
    assert second.action == "none"  # 0.6 < 1.0 when it reserved
    third_check = enforcer.check({})
    assert third_check.state == "exceeded"  # 1.2 held
    enforcer.commit(first, 0.1)  # settles at the actual cost
    enforcer.release(second)  # failed call: nothing added
    assert enforcer.check({}).statuses[0].spent_usd == pytest.approx(0.1)
    assert enforcer.store.read([first.statuses[0].key]) == [(pytest.approx(0.1), 0.0)]


def test_reservation_is_an_atomic_check_for_block_budgets():
    enforcer = _enforcer(_budget(mode="block", limit_usd=1.0))
    stale = enforcer.check({})  # read before another call reserved
    enforcer.reserve(enforcer.check({}), 1.0)
    blocked = enforcer.reserve(stale, 0.01)
    assert blocked.action == "block" and blocked.blocked_by.state == "exceeded"
    with pytest.raises(BudgetExceeded):
        enforcer.raise_if_blocked(blocked)


def test_unpriced_call_settles_at_its_estimate():
    enforcer = _enforcer(_budget(mode="warn", limit_usd=1.0))
    held = enforcer.reserve(enforcer.check({}), 0.25)
    enforcer.commit(held, None)
    assert enforcer.check({}).statuses[0].spent_usd == pytest.approx(0.25)


def test_reservations_lapse():
    now = [1000.0]
    enforcer = BudgetEnforcer(_doc(_budget(mode="block", limit_usd=1.0)),
                              InProcessBudgetStore(clock=lambda: now[0]),
                              clock=lambda: NOW, reservation_ttl_seconds=5)
    enforcer.reserve(enforcer.check({}), 2.0)
    assert enforcer.check({}).state == "exceeded"
    now[0] += 6
    assert enforcer.check({}).state == "ok"


def test_store_without_reservations_still_works():
    class Plain:
        blocking = False

        def __init__(self):
            self.values = {}

        def get_many(self, keys):
            return [self.values.get(k, 0.0) for k in keys]

        def add(self, key, amount, ttl_seconds):
            self.values[key] = self.values.get(key, 0.0) + amount
            return self.values[key]

    enforcer = _enforcer(_budget(mode="block", limit_usd=1.0), store=Plain())
    assert not enforcer.reserves
    check = enforcer.reserve(enforcer.check({}), 0.5)
    assert check.reservation_id is None
    enforcer.commit(check, 1.0)
    assert enforcer.check({}).action == "block"


def test_redis_reservations_use_the_gateway_format():
    fakeredis = _fakeredis()
    client = fakeredis.FakeRedis()
    enforcer = _enforcer(_budget(mode="block", limit_usd=1.0),
                         store=RedisBudgetStore(client))
    held = enforcer.reserve(enforcer.check({}), 0.25, 10)
    key = held.statuses[0].key
    (member,) = client.zrange(reservations_key(key), 0, -1)
    assert member.decode() == f"{held.reservation_id}|0.25"
    assert reservations_key(key) == "{" + key + "}:res"
    # A gateway reservation on the same counter counts for the SDK too.
    client.zadd(reservations_key(key), {"gw-call|0.5": 10**13})
    assert enforcer.check({}).statuses[0].spent_usd == pytest.approx(0.75)
    enforcer.commit(held, 0.1)
    assert float(client.get(key)) == pytest.approx(0.1)
    assert client.ttl(key) > 0
    assert client.zrange(reservations_key(key), 0, -1) == [b"gw-call|0.5"]


async def test_async_router_reserves_and_releases_on_failure():
    enforcer = _enforcer(_budget(mode="block", limit_usd=10))
    router = _det_router(enforcer, cls=AsyncShieldsRouter)
    _, upstreams = _wire(router, is_async=True)

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    for fake in upstreams.values():
        fake.chat.completions.create = boom
    with pytest.raises(RuntimeError):
        await router.chat.completions.create(messages=HI, model="mini", max_tokens=10)
    key = enforcer.check({}).statuses[0].key
    assert enforcer.store.read([key]) == [(0.0, 0.0)]


def test_router_team_budget_from_ps_metadata():
    enforcer = _enforcer(_budget(scope="team", subject="data", mode="block",
                                 limit_usd=1e-9))
    router = _det_router(enforcer)
    events, _ = _wire(router)
    router.chat.completions.create(messages=HI, model="mini", max_tokens=10,
                                   ps_metadata={"team": "data"})
    assert events[0]["budget_state"] == "ok"
    key = enforcer.check({"team": "data"}).statuses[0].key
    assert enforcer.store.read([key]) == [(pytest.approx(events[0]["cost"]), 0.0)]
    with pytest.raises(BudgetExceeded):
        router.chat.completions.create(messages=HI, model="mini", max_tokens=10,
                                       ps_metadata={"team": "data"})
    router.chat.completions.create(messages=HI, model="mini", max_tokens=10,
                                   ps_metadata={"team": "other"})


async def test_async_client_releases_on_upstream_error():
    enforcer = _enforcer(_budget(mode="block", limit_usd=10))
    client, _ = _client(enforcer, cls=AsyncShieldsClient)

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    client._upstream.chat.completions.create = boom
    with pytest.raises(RuntimeError):
        await client.chat.completions.create(model="gpt-4o-mini", messages=HI)
    key = enforcer.check({}).statuses[0].key
    assert enforcer.store.read([key]) == [(0.0, 0.0)]


# --- threshold notifications ------------------------------------------------------


def test_threshold_callback_fires_once_per_crossing():
    seen: list[BudgetThreshold] = []
    enforcer = BudgetEnforcer(
        _doc(_budget(scope="team", subject="data", mode="warn", limit_usd=1.0,
                     warn_at=0.5)),
        clock=lambda: NOW, on_threshold=seen.append,
    )
    for _ in range(4):
        check = enforcer.reserve(enforcer.check({"team": "data"}), 0.3)
        enforcer.commit(check, 0.3)
    assert [(e.threshold, e.spent) for e in seen] == [
        ("warn", pytest.approx(0.6)), ("exceeded", pytest.approx(1.2))]
    ev = seen[0]
    assert (ev.budget_id, ev.scope, ev.subject, ev.window, ev.unit, ev.limit,
            ev.mode, ev.period) == ("b", "team", "data", "day", "usd", 1.0, "warn",
                                    "2026-11-06")
    assert ev.as_dict()["type"] == "budget.threshold_crossed"


def test_one_write_crossing_both_thresholds_reports_both(caplog):
    seen: list[BudgetThreshold] = []
    enforcer = BudgetEnforcer(_doc(_budget(mode="block", limit_usd=1.0)),
                              clock=lambda: NOW, on_threshold=seen.append)
    with caplog.at_level("WARNING", logger="prompt_shields.budgets"):
        enforcer.commit(enforcer.check({}), 5.0)
    assert [e.threshold for e in seen] == ["warn", "exceeded"]
    assert "crossed exceeded" in caplog.text


def test_threshold_callback_errors_never_fail_the_call():
    def bad(_):
        raise RuntimeError("pager down")

    enforcer = BudgetEnforcer(_doc(_budget(mode="warn")), clock=lambda: NOW,
                              on_threshold=bad)
    enforcer.commit(enforcer.check({}), 5.0)
    assert enforcer.check({}).state == "exceeded"


def test_threshold_reported_by_exactly_one_process_on_a_shared_redis():
    fakeredis = _fakeredis()
    server = fakeredis.FakeServer()
    seen: list[BudgetThreshold] = []
    doc = _doc(_tokens_budget(scope="project", subject="*", mode="warn",
                              limit_tokens=1000))
    enforcers = [
        BudgetEnforcer(doc, RedisBudgetStore(fakeredis.FakeRedis(server=server)),
                       clock=lambda: NOW, on_threshold=seen.append)
        for _ in range(4)
    ]

    def work(e):
        for _ in range(25):
            check = e.reserve(e.check({"project": "chat"}), tokens=10)
            e.commit(check, None, 10)

    threads = [threading.Thread(target=work, args=(e,)) for e in enforcers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    key = counter_key("default", "project", "chat", "day", "2026-11-06", "tokens")
    assert enforcers[0].store.read([key]) == [(1000.0, 0.0)]
    assert sorted(e.threshold for e in seen) == ["exceeded", "warn"]


def test_in_process_store_is_thread_safe_with_reservations():
    seen: list[BudgetThreshold] = []
    enforcer = BudgetEnforcer(_doc(_budget(mode="warn", limit_usd=10.0)),
                              clock=lambda: NOW, on_threshold=seen.append)

    def work():
        for _ in range(200):
            check = enforcer.reserve(enforcer.check({}), 0.05)
            enforcer.commit(check, 0.01)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    key = enforcer.check({}).statuses[0].key
    assert enforcer.store.read([key]) == [(pytest.approx(16.0), 0.0)]
    assert sorted(e.threshold for e in seen) == ["exceeded", "warn"]
