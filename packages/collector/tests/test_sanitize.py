"""collector.sanitize: the shared check for strings the database cannot
store, and the JSON depth limit. Route behaviour is in
test_unstorable_input_db.py."""

from __future__ import annotations

import json

import pytest
from collector.sanitize import (
    MAX_JSON_DEPTH,
    BodyTooDeep,
    is_storable,
    is_storable_text,
    json_depth,
    loads_bounded,
    replace_unstorable,
    storable_text,
)


def nested(n: int) -> str:
    return "[" * n + "]" * n


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("gpt-4o", True),
        ("", True),
        ("café \U0001f600", True),  # astral character: a joined pair
        ("a\u0000b", False),
        # Lone surrogates, built with chr() (ruff reads the escapes as equal).
        ("a" + chr(0xD800) + "b", False),
        ("a" + chr(0xDFFF) + "b", False),
        # Two code points that would form a pair, built at run time so they
        # stay unjoined: not encodable either.
        (chr(0xD83D) + chr(0xDE00), False),
    ],
)
def test_is_storable_text(value, ok):
    assert is_storable_text(value) is ok
    assert storable_text(value) == (value if ok else None)


def test_json_loads_joins_escaped_pairs_and_keeps_lone_surrogates():
    assert is_storable_text(json.loads('"\\ud83d\\ude00"'))
    assert not is_storable_text(json.loads('"\\ud83d"'))


def test_storable_text_is_none_for_non_strings():
    assert storable_text(None) is None
    assert storable_text(5) is None


@pytest.mark.parametrize(
    "value",
    [
        {"a": "x\u0000"},
        {"x\u0000": 1},  # an object key is stored too
        ["ok", ["x\ud800"]],
        [{"deep": [{"key\udc00": None}]}],
    ],
)
def test_is_storable_checks_every_string_and_key(value):
    assert not is_storable(value)


def test_is_storable_accepts_ordinary_json_and_scalars():
    assert is_storable({"a": [1, 2.5, None, True, "x"], "b": {"c": "d"}})
    assert is_storable(7) and is_storable(None) and is_storable("x")


def test_is_storable_bounds_depth():
    at_limit = json.loads(nested(MAX_JSON_DEPTH))
    assert is_storable(at_limit)
    assert not is_storable(json.loads(nested(MAX_JSON_DEPTH + 1)))
    assert not is_storable([[1]], max_depth=1)


def test_json_depth():
    assert json_depth(1) == 0
    assert json_depth([]) == 1
    assert json_depth({"a": [{"b": 1}], "c": 2}) == 3
    # Iterative: deeper than the recursion limit is fine to measure.
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    assert json_depth(deep) == 5001


def test_loads_bounded_limits_depth():
    assert loads_bounded(nested(MAX_JSON_DEPTH)) == json.loads(nested(MAX_JSON_DEPTH))
    with pytest.raises(BodyTooDeep):
        loads_bounded(nested(MAX_JSON_DEPTH + 1))
    # Past the recursion limit json.loads raises RecursionError, not
    # ValueError; it is the same 400.
    with pytest.raises(BodyTooDeep):
        loads_bounded(nested(100_000))


@pytest.mark.parametrize("raw", [b"{", b"\xff\xfe", "not json"])
def test_loads_bounded_invalid_json_is_value_error(raw):
    with pytest.raises(ValueError) as info:
        loads_bounded(raw)
    assert not isinstance(info.value, BodyTooDeep)


def test_replace_unstorable():
    out = replace_unstorable({"k\u0000": ["a\ud800", 1, ("b",)]})
    assert out == {"k�": ["a�", 1, ["b"]]}
    json.dumps(out, ensure_ascii=False).encode("utf-8")  # encodable now
