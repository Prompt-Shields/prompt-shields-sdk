"""The Python snippets in ``docs/guides/routing.mdx`` run and give the
results the guide shows.

Every ``python`` block that sends no call is run in order, in one namespace,
from the repository root. A line ``expr  # => value`` (or ``expr`` followed
by a ``# => value`` line) is checked as ``assert expr == value``.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs" / "guides" / "routing.mdx"
_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
_INLINE = re.compile(r"^(\s*)(\S.*?)\s+# => (.+)$")
_ALONE = re.compile(r"^(\s*)# => (.+)$")


def _blocks() -> list[str]:
    blocks = _BLOCK.findall(GUIDE.read_text(encoding="utf-8"))
    # Blocks that call a provider need keys and network.
    return [b for b in blocks if "chat.completions.create" not in b]


def _with_asserts(block: str) -> tuple[str, int]:
    out: list[str] = []
    checks = 0
    for line in block.splitlines():
        alone = _ALONE.match(line)
        if alone:
            indent, expected = alone.groups()
            expr = out.pop().strip()
            out.append(f"{indent}assert ({expr}) == ({expected}), ({expr})")
            checks += 1
            continue
        inline = _INLINE.match(line)
        if inline:
            indent, expr, expected = inline.groups()
            out.append(f"{indent}assert ({expr}) == ({expected}), ({expr})")
            checks += 1
            continue
        out.append(line)
    return "\n".join(out), checks


def test_guide_has_runnable_snippets():
    assert len(_blocks()) >= 4


def test_routing_guide_snippets(monkeypatch):
    monkeypatch.chdir(ROOT)
    namespace: dict = {}
    total = 0
    for block in _blocks():
        source, checks = _with_asserts(block)
        ast.parse(source)
        total += checks
        try:
            exec(compile(source, str(GUIDE), "exec"), namespace)  # noqa: S102
        except AssertionError as exc:
            pytest.fail(f"guide snippet result differs: {exc}\n---\n{block}")
    assert total >= 10, "the guide's results are no longer checked"
    assert os.getcwd() == str(ROOT)
