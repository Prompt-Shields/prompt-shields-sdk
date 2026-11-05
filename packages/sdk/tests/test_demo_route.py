"""Smoke test: ``demo/demo_route.py`` runs offline and shows what the docs say.

The demo's stubs answer in-process and its collector listens on 127.0.0.1,
so this needs no keys and no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SDK_DIR = str(ROOT / "packages" / "sdk")


def test_demo_route_runs_offline():
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy") and not k.startswith("PS_")
           and k != "PYTHONPATH"}
    env.update(PYTHONPATH=SDK_DIR, NO_PROXY="*")
    result = subprocess.run(
        [sys.executable, str(ROOT / "demo" / "demo_route.py")],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    # Sensitivity routing: the default level, PII escalation, fail closed.
    assert "sensitivity confidential (default)" in out
    assert "-> mistral-small-eu via mistral-eu (EEA)" in out
    assert "sensitivity restricted (pii_escalated)" in out
    assert "PolicyViolation" in out
    assert "requests sent to any provider: 0" in out
    # Failover is not done yet (#21): one attempt, on the chosen provider only.
    assert "requests per provider: {'scaleway': 1}" in out
    # Savings and telemetry.
    assert "Saved " in out
    assert "stub collector received 5 events" in out
