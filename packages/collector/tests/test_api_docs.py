"""FastAPI's /docs, /redoc and /openapi.json are off unless
PS_COLLECTOR_API_DOCS_ENABLED is set: they are unauthenticated and list
every route, including operator routes such as tenant erasure."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import textwrap

import httpx
import pytest
from collector import app as app_module
from collector.app import API_DOCS_WARNING, app, lifespan
from collector.config import Settings, settings

DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


def test_setting_defaults_off(monkeypatch):
    monkeypatch.delenv("PS_COLLECTOR_API_DOCS_ENABLED", raising=False)
    assert Settings().collector_api_docs_enabled is False
    monkeypatch.setenv("PS_COLLECTOR_API_DOCS_ENABLED", "true")
    assert Settings().collector_api_docs_enabled is True


@pytest.mark.asyncio
async def test_docs_off_by_default():
    assert settings.collector_api_docs_enabled is False
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        for path in DOCS_PATHS:
            assert (await c.get(path)).status_code == 404, path


def test_in_process_schema_still_built():
    # tests/test_openapi_spec.py relies on this.
    assert "delete" in app.openapi()["paths"]["/api/v1/operator/tenants/{tenant_id}"]


def test_docs_served_when_enabled():
    # The URLs are fixed when collector.app is imported, so import it fresh.
    script = textwrap.dedent(
        """
        import asyncio, httpx
        from collector.app import app

        async def main():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
                for path in ("/docs", "/redoc", "/openapi.json"):
                    r = await c.get(path)
                    assert r.status_code == 200, (path, r.status_code)
                spec = (await c.get("/openapi.json")).json()
                assert "/partner/v1/health" in spec["paths"]

        asyncio.run(main())
        """
    )
    env = {**os.environ, "PS_COLLECTOR_API_DOCS_ENABLED": "true"}
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_startup_warns_when_enabled(monkeypatch, caplog):
    monkeypatch.setattr(settings, "collector_api_docs_enabled", True)
    monkeypatch.setattr(settings, "rls_required", False)
    monkeypatch.setattr(settings, "webhook_worker_interval_seconds", 0)
    monkeypatch.setattr(settings, "provider_usage_pull_interval_seconds", 0)
    monkeypatch.setattr(app_module, "get_embedder", lambda: None)
    with caplog.at_level(logging.WARNING, logger="prompt_shields.collector"):
        async with lifespan(app):
            pass
    assert API_DOCS_WARNING in caplog.text
