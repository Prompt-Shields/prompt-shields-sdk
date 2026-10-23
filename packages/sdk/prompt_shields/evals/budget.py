"""Spend cap and rate limit for re-execution.

The spend cap is checked **before** each call against a worst-case estimate
(an upper bound on the prompt's input tokens, see
:func:`worst_case_input_tokens`, plus ``max_tokens`` of output, at list
price), so
a run never starts a call that could take it over the cap. After the call the
actual cost is booked; when the provider reports no cost, the worst-case
estimate is booked instead.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any


class SpendCap:
    """Running spend against a USD cap. ``max_usd=None`` means no cap."""

    def __init__(self, max_usd: float | None):
        # NaN would make every comparison False and so allow every call.
        if max_usd is not None and not (math.isfinite(max_usd) and max_usd >= 0):
            raise ValueError("max_usd must be a finite number >= 0")
        self.max_usd = max_usd
        self.spent = 0.0
        self.refused = 0

    def allows(self, worst_case_usd: float) -> bool:
        if self.max_usd is None:
            return True
        if self.spent + worst_case_usd > self.max_usd + 1e-12:
            self.refused += 1
            return False
        return True

    def book(self, actual_usd: float | None, worst_case_usd: float) -> None:
        self.spent += actual_usd if actual_usd is not None else worst_case_usd

    @property
    def remaining(self) -> float | None:
        return None if self.max_usd is None else max(self.max_usd - self.spent, 0.0)


class RateLimiter:
    """At most ``requests_per_minute`` calls, spaced evenly. ``None`` = no limit.

    ``clock`` and ``sleep`` are injectable so tests never wait.
    """

    def __init__(
        self,
        requests_per_minute: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if requests_per_minute is not None and not (
            math.isfinite(requests_per_minute) and requests_per_minute > 0
        ):
            raise ValueError("requests_per_minute must be a finite number > 0")
        self._interval = 60.0 / requests_per_minute if requests_per_minute else 0.0
        self._clock = clock
        self._sleep = sleep
        self._next: float | None = None
        self.waited = 0.0

    def acquire(self) -> None:
        if not self._interval:
            return
        now = self._clock()
        if self._next is not None and now < self._next:
            wait = self._next - now
            self._sleep(wait)
            self.waited += wait
            now = self._next
        self._next = now + self._interval


def worst_case_input_tokens(messages: Sequence[Any]) -> int | None:
    """An upper bound on the input tokens of ``messages``, or None if none is known.

    Byte-level BPE tokenizers never emit a token for less than one byte, so the
    UTF-8 size of the serialised messages (which also covers roles, tool calls
    and per-message overhead) bounds the token count. The ~4 characters per
    token heuristic does not: CJK text is about one token per character. A
    content part that is not text (an image, audio, a file) has no bound from
    its size, so a message list with one returns None.
    """
    for message in messages or []:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, Mapping) or part.get("type", "text") != "text":
                    return None
    return len(json.dumps(list(messages or []), ensure_ascii=False,
                          default=str).encode("utf-8"))
