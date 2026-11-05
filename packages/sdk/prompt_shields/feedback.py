"""Per-call ids and explicit feedback (#43).

Every call made through a Prompt Shields client gets a ``call_id``: a UUID
generated before the call (or the caller's own, from
``ps_metadata={"call_id": ...}``). It is

  - on the call's telemetry event (``call_id``),
  - sent as the ``X-PS-Call-Id`` header on calls that already carry
    ``X-PS-*`` headers (route hints: the call goes to a Prompt Shields
    gateway), so the gateway reports the call under the same id; a call
    straight to a provider gets no ``X-PS-*`` header,
  - returned to the caller: ``response.ps_call_id`` (a plain attribute, not
    part of the response's fields or its ``model_dump()``), the same
    attribute on a wrapped stream, and ``last_call_id()`` for the current
    thread or task.

``client.feedback(call_id, score, reason=None)`` (or ``send_feedback``)
then records how good the answer was, for the routing learner. Metadata
only: a score from 0 (bad) to 1 (good), and optionally one reason label from
``FEEDBACK_REASONS`` and the caller's opaque user id. There is no field for
free text. The collector keeps one feedback per call (a later one replaces
it) and rate-limits it per tenant and per user.

Unlike telemetry, feedback is not fail-open: it is an explicit request, so
an invalid argument raises ``ValueError`` (``TypeError`` for a score that
is not a number) before anything is sent, and a
refused or failed request raises ``FeedbackError``.
"""

from __future__ import annotations

import contextlib
import math
import re
import uuid
from contextvars import ContextVar
from typing import Any

import httpx

FEEDBACK_PATH = "/ingest/feedback"
CALL_ID_HEADER = "X-PS-Call-Id"
DEFAULT_TIMEOUT = 10.0
MAX_USER_ID_LENGTH = 128
# Kept in step with the collector (collector/feedback.py REASONS).
FEEDBACK_REASONS = (
    "correct",
    "helpful",
    "incorrect",
    "incomplete",
    "hallucination",
    "off_topic",
    "refusal",
    "format_error",
    "unsafe",
    "too_slow",
    "too_long",
    "other",
)

_CALL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_LAST_CALL_ID: ContextVar[str | None] = ContextVar("ps_last_call_id", default=None)


class FeedbackError(Exception):
    """The collector refused the feedback, or could not be reached.

    ``status_code`` is None for a network error. ``retry_after`` (seconds)
    is set on a 429.
    """

    def __init__(self, message: str, status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def is_call_id(value: Any) -> bool:
    return isinstance(value, str) and _CALL_ID_RE.fullmatch(value) is not None


def new_call_id(ps_metadata: Any = None) -> str:
    """The id for a new call: the caller's ``ps_metadata["call_id"]`` when it
    is a valid id (1-128 of ``A-Za-z0-9._:-``), else a fresh UUID. It also
    becomes ``last_call_id()`` in the current context."""
    supplied = ps_metadata.get("call_id") if isinstance(ps_metadata, dict) else None
    call_id = supplied if is_call_id(supplied) else str(uuid.uuid4())
    _LAST_CALL_ID.set(call_id)
    return call_id


def last_call_id() -> str | None:
    """The call_id of the latest call started in this thread or asyncio
    task (None before the first)."""
    return _LAST_CALL_ID.get()


def attach_call_id(obj: Any, call_id: str) -> Any:
    """Set ``obj.ps_call_id`` without touching the object's fields
    (pydantic models keep it out of ``model_dump()``). Best effort: an
    object that takes no attributes is returned unchanged."""
    # Fail-open: the call already succeeded.
    with contextlib.suppress(Exception):
        object.__setattr__(obj, "ps_call_id", call_id)
    return obj


def call_id_headers(kwargs: dict, call_id: str) -> None:
    """Add ``X-PS-Call-Id`` to a call's ``extra_headers`` when it already
    carries ``X-PS-*`` headers (route hints or gateway context: the call goes
    to a Prompt Shields gateway). A call straight to a provider gets no
    ``X-PS-*`` header."""
    headers = kwargs.get("extra_headers") or {}
    if any(str(k).lower().startswith("x-ps-") for k in headers):
        kwargs["extra_headers"] = {**headers, CALL_ID_HEADER: call_id}


def build_feedback(call_id: str, score: float | bool, reason: str | None = None,
                   user_id: str | None = None) -> dict:
    """The request body for one feedback; ValueError (TypeError for a
    non-numeric score) when an argument is invalid. ``score`` is 0..1, or a bool (True = 1, good)."""
    if not is_call_id(call_id):
        raise ValueError("call_id must be 1-128 characters of A-Z a-z 0-9 . _ : -")
    if isinstance(score, bool):
        score = 1.0 if score else 0.0
    if not isinstance(score, (int, float)):
        raise TypeError("score must be a number from 0 to 1")
    score = float(score)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        raise ValueError("score must be a number from 0 to 1")
    body: dict = {"call_id": call_id, "score": score, "source": "sdk"}
    if reason is not None:
        if reason not in FEEDBACK_REASONS:
            raise ValueError(
                f"reason must be one of {', '.join(FEEDBACK_REASONS)} "
                "(feedback never carries free text)"
            )
        body["reason"] = reason
    if user_id is not None:
        if (
            not isinstance(user_id, str)
            or not user_id
            or len(user_id) > MAX_USER_ID_LENGTH
            or not user_id.isprintable()
        ):
            raise ValueError(f"user_id must be 1-{MAX_USER_ID_LENGTH} printable characters")
        body["user_id"] = user_id
    return body


def _url(collector_url: str) -> str:
    return f"{collector_url.rstrip('/')}{FEEDBACK_PATH}"


def _result(resp: httpx.Response) -> dict:
    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError:
            return {}
    retry_after = None
    if resp.status_code == 429:
        try:
            retry_after = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            retry_after = None
    detail = ""
    with contextlib.suppress(Exception):  # not JSON
        body = resp.json().get("detail")
        detail = body.get("detail", "") if isinstance(body, dict) else str(body or "")
    raise FeedbackError(
        f"Feedback refused ({resp.status_code}){': ' + detail if detail else ''}",
        status_code=resp.status_code,
        retry_after=retry_after,
    )


def send_feedback(call_id: str, score: float | bool, reason: str | None = None, *,
                  ps_api_key: str, ps_collector_url: str = "http://localhost:8000",
                  user_id: str | None = None,
                  timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Send feedback on one call to the collector. Returns its answer
    (``{"call_id", "created", "matched"}``); raises ValueError or
    FeedbackError (see the module docstring)."""
    body = build_feedback(call_id, score, reason, user_id)
    try:
        resp = httpx.post(_url(ps_collector_url), json=body, timeout=timeout,
                          headers={"Authorization": f"Bearer {ps_api_key}"})
    except httpx.HTTPError as e:
        raise FeedbackError(f"Feedback not sent: {type(e).__name__}") from e
    return _result(resp)


async def send_feedback_async(call_id: str, score: float | bool,
                              reason: str | None = None, *, ps_api_key: str,
                              ps_collector_url: str = "http://localhost:8000",
                              user_id: str | None = None,
                              timeout: float = DEFAULT_TIMEOUT) -> dict:
    """``send_feedback`` for asyncio."""
    body = build_feedback(call_id, score, reason, user_id)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                _url(ps_collector_url), json=body,
                headers={"Authorization": f"Bearer {ps_api_key}"},
            )
    except httpx.HTTPError as e:
        raise FeedbackError(f"Feedback not sent: {type(e).__name__}") from e
    return _result(resp)
