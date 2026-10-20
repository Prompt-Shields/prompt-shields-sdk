"""Client values the database can store, and JSON bodies of bounded depth.

Postgres ``text`` and ``jsonb`` cannot hold NUL (U+0000), and a lone UTF-16
surrogate (U+D800-U+DFFF, which ``json.loads`` accepts from a ``"\\ud800"``
escape) cannot be encoded as UTF-8. Either one, anywhere in a write, fails
the whole transaction. On an ingest route that is a 500 for the batch: the
sender retries it until it gives up, and the good events in it are lost.

* Ingest routes (``/ingest/events``, ``/ingest/otel/v1/traces``,
  ``/ingest/kong``, ``/ingest/mcp``) drop such a value per field, as if it
  had not been sent, and report it as ``<field>:invalid`` where the route
  reports invalid fields. An event that cannot be stored without the value
  (its vendor) is skipped and counted; the rest of the batch is stored.
* Admin and partner write routes refuse it with a 422 (``StorableStr``,
  ``StorableJson``): a person is there to fix the input.

Depth. ``json.loads`` raises ``RecursionError`` (not ``ValueError``) past
about a thousand levels, and every level below that is accepted. A JSON body
nested deeper than ``MAX_JSON_DEPTH`` gets a 400 on every route that reads
one: ``loads_bounded`` for the receivers that parse their own body, and
``BoundedJsonRoute`` for routes whose body FastAPI parses.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from pydantic import AfterValidator

# Containers (objects and arrays) a JSON body may nest. The deepest real
# payload is OTLP: about ten levels to a span attribute, plus two or three
# per nested AnyValue.
MAX_JSON_DEPTH = 64
TOO_DEEP_DETAIL = f"JSON body is nested deeper than {MAX_JSON_DEPTH} levels"
UNSTORABLE_MESSAGE = "must not contain NUL characters or unpaired surrogates"

_UNSTORABLE = re.compile("[\x00\ud800-\udfff]")


def is_storable_text(value: str) -> bool:
    """No NUL and no surrogate code point. ``json.loads`` joins a valid
    surrogate pair into one character, so any surrogate left is unpaired."""
    return _UNSTORABLE.search(value) is None


def storable_text(value: Any) -> str | None:
    """``value`` if it is a string the database can store, else None."""
    return value if isinstance(value, str) and is_storable_text(value) else None


def json_depth(value: Any) -> int:
    """How many objects and arrays nest in a parsed JSON value (a scalar is
    0, ``[]`` is 1). Iterative, so any depth is safe to measure."""
    deepest = 0
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children)
    return deepest


def is_storable(value: Any, max_depth: int = MAX_JSON_DEPTH) -> bool:
    """True when every string in a JSON value, object keys included, is
    storable and the value nests no deeper than ``max_depth``."""
    stack = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            if not is_storable_text(item):
                return False
        elif isinstance(item, (dict, list)):
            if depth >= max_depth:
                return False
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(key, str) and not is_storable_text(key):
                        return False
                    stack.append((child, depth + 1))
            else:
                stack.extend((child, depth + 1) for child in item)
    return True


def replace_unstorable(value: Any) -> Any:
    """``value`` with every unstorable character replaced by U+FFFD, for
    echoing client input in an error response (a lone surrogate cannot be
    encoded, so the response itself would fail with a 500)."""
    if isinstance(value, str):
        return _UNSTORABLE.sub("�", value)
    if isinstance(value, dict):
        return {replace_unstorable(k): replace_unstorable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [replace_unstorable(v) for v in value]
    return value


class BodyTooDeep(ValueError):
    """A JSON body nested deeper than MAX_JSON_DEPTH."""


def loads_bounded(raw: bytes | str) -> Any:
    """``json.loads`` with the depth limit. Raises ``BodyTooDeep`` for a body
    nested deeper than ``MAX_JSON_DEPTH``, and ``ValueError`` (or
    ``UnicodeDecodeError``, a subclass) for one that is not JSON."""
    try:
        value = json.loads(raw)
    except RecursionError:
        raise BodyTooDeep(TOO_DEEP_DETAIL) from None
    if json_depth(value) > MAX_JSON_DEPTH:
        raise BodyTooDeep(TOO_DEEP_DETAIL)
    return value


def too_deep_problem() -> HTTPException:
    return HTTPException(
        status_code=400,
        detail={
            "type": "about:blank",
            "title": "Bad Request",
            "status": 400,
            "detail": TOO_DEEP_DETAIL,
        },
    )


def _require_storable(value: Any) -> Any:
    if not is_storable(value):
        raise ValueError(UNSTORABLE_MESSAGE)
    return value


# Request-body types for admin and partner routes: a value the database
# cannot store is a 422, not a 500.
StorableStr = Annotated[str, AfterValidator(_require_storable)]
StorableJson = Annotated[dict, AfterValidator(_require_storable)]


def _is_json(content_type: str | None) -> bool:
    """FastAPI parses a body as JSON when it has no content type or an
    ``application/json`` / ``application/*+json`` one."""
    if not content_type:
        return True
    media = content_type.split(";")[0].strip().lower()
    return media == "application/json" or (
        media.startswith("application/") and media.endswith("+json")
    )


class BoundedJsonRoute(APIRoute):
    """Refuses a JSON body nested deeper than ``MAX_JSON_DEPTH`` with a 400
    before FastAPI parses it. Without this, FastAPI answers a very deep body
    with a generic 400 and accepts anything shallower than Python's
    recursion limit. Invalid JSON is left to FastAPI's own 422."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            body = await request.body()
            if body and _is_json(request.headers.get("content-type")):
                try:
                    value = loads_bounded(body)
                except BodyTooDeep:
                    raise too_deep_problem() from None
                except ValueError:
                    pass
                else:
                    # Starlette's parsed-body cache: FastAPI reads it
                    # instead of parsing the body a second time.
                    request._json = value
            return await handler(request)

        return bounded_handler
