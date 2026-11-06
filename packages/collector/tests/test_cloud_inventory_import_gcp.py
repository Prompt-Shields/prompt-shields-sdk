"""Offline import of a Google Cloud Vertex AI inventory export (#94), with
no cloud credentials. The export is built from recorded `gcloud ai` output
in tests/fixtures/cloud_inventory/gcp (shapes from the Vertex AI REST
reference); no cloud is called."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from collector import cloud_inventory as ci
from collector import cloud_inventory_import as imp
from collector.app import app
from db.models import AIAsset, Tenant
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.test_cloud_inventory_gcp import _key
from tests.test_cloud_inventory_import import OP_TOKEN, _no_sleep, _view

FIXTURES = Path(__file__).parent / "fixtures" / "cloud_inventory" / "gcp"
SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "cloud-inventory-export-gcp.sh"
)
PROJECT = "acme-ml-prod"
REGIONS = ("europe-west4", "us-central1")
EW4 = "projects/123456789012/locations/europe-west4"
UC1 = "projects/123456789012/locations/us-central1"
OWNER_EMAILS = {"claims-team": "claims-ai@acme.com", "support-team": "support@acme.com"}
SECRET = "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def fixture_export(regions=REGIONS) -> dict:
    """The recorded `gcloud` output, unchanged, in the export layout."""
    return {
        "cloud": "gcp",
        "owner_label_emails": OWNER_EMAILS,
        "projects": {
            PROJECT: {
                r: {
                    "endpoints": _load(f"{PROJECT}.{r}.endpoints.json"),
                    "models": _load(f"{PROJECT}.{r}.models.json"),
                }
                for r in regions
            }
        },
    }


GEMMA = f"{EW4}/endpoints/9001112223334445556/deployedModels/4417203958122938368"
FRAUD = f"{EW4}/endpoints/9001112223334445556/deployedModels/5520117736011808768"
ROUTER = f"{UC1}/endpoints/9501112223334445556/deployedModels/7001234567890123456"
EXPECTED = {
    # resource -> (model, region, kind, owner)
    GEMMA: ("gemma2", "europe-west4", "model_garden", "claims-ai@acme.com"),
    # Served in europe-west4 from a model registered in us-central1; the
    # endpoint's owner label overrides the model's.
    FRAUD: ("fraud-xgb-v2", "europe-west4", "custom", "claims-ai@acme.com"),
    ROUTER: ("support-router", "us-central1", "custom", "support@acme.com"),
}


def test_export_becomes_deployments_with_region_and_owner():
    summary, deployments = imp.parse_gcp_export(fixture_export())
    assert _view(deployments) == EXPECTED
    assert summary == {"projects": 1, "regions": 2}
    by_id = {d.resource_id: d for d in deployments}
    assert by_id[GEMMA].tags["department"] == "claims"
    assert by_id[FRAUD].tags["department"] == "risk"
    assert ci.environment_from_tags(by_id[ROUTER].tags) == "staging"


def test_a_model_in_an_unexported_region_keeps_its_display_name():
    summary, deployments = imp.parse_gcp_export(fixture_export(("europe-west4",)))
    view = _view(deployments)
    assert view[FRAUD] == ("fraud-xgb", "europe-west4", "custom", "claims-ai@acme.com")
    assert summary == {"projects": 1, "regions": 1, "models_in_unexported_regions": 1}


def test_tuning_jobs_listing_adds_tuned_models():
    export = fixture_export()
    export["projects"][PROJECT]["europe-west4"]["tuning_jobs"] = {
        "tuningJobs": [
            {
                "name": f"{EW4}/tuningJobs/1",
                "state": "JOB_STATE_SUCCEEDED",
                "baseModel": "gemini-2.0-flash-001",
                "tunedModel": {"model": f"{EW4}/models/777@1"},
                "labels": {"owner": "claims-team"},
            },
            {
                "name": f"{EW4}/tuningJobs/2",
                "state": "JOB_STATE_FAILED",
                "tunedModel": {"model": f"{EW4}/models/778@1"},
            },
        ]
    }
    _, deployments = imp.parse_gcp_export(export)
    assert _view(deployments) == {
        **EXPECTED,
        f"{EW4}/models/777": (
            "gemini-2.0-flash-001",
            "europe-west4",
            "tuned_model",
            "claims-ai@acme.com",
        ),
    }


# ── The live pull, served the same recorded data ───────────────────────


def _vertex_replay(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    region = request.url.host.removesuffix("-aiplatform.googleapis.com")
    prefix = f"/v1/projects/{PROJECT}/locations/{region}/"
    collection = request.url.path.removeprefix(prefix)
    if collection == "tuningJobs":
        return httpx.Response(200, json={})
    if collection in ("endpoints", "models"):
        return httpx.Response(
            200,
            json={collection: _load(f"{PROJECT}.{region}.{collection}.json")},
        )
    return httpx.Response(404)


def _creds() -> ci.GcpInventoryCredentials:
    return ci.GcpInventoryCredentials(
        service_account=_key(),
        project_ids=[PROJECT],
        regions=list(REGIONS),
        owner_label_emails=OWNER_EMAILS,
    )


async def test_import_and_live_pull_agree_on_the_same_data():
    _, pulled = await ci.fetch_gcp_deployments(
        _creds(), transport=httpx.MockTransport(_vertex_replay), sleep=_no_sleep
    )
    _, imported = imp.parse_gcp_export(fixture_export())
    assert _view(pulled) == _view(imported) == EXPECTED
    tenant = uuid.uuid4()
    assert {ci.asset_id_for(tenant, d) for d in pulled} == {
        ci.asset_id_for(tenant, d) for d in imported
    }
    assert {d.resource_id: d.tags for d in pulled} == {
        d.resource_id: d.tags for d in imported
    }


def test_rest_shaped_listings_are_accepted_too():
    export = fixture_export()
    for region in export["projects"][PROJECT].values():
        region["endpoints"] = {"endpoints": region["endpoints"]}
        region["models"] = {"models": region["models"], "nextPageToken": ""}
    _, deployments = imp.parse_gcp_export(export)
    assert _view(deployments) == EXPECTED


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda e: e.update(projects={}), "at least one project"),
        (lambda e: e.update(projects={"Acme_Prod": {"us-central1": {}}}), "project"),
        (lambda e: e.update(projects={PROJECT: {}}), "at least one region"),
        (lambda e: e["projects"][PROJECT].update({"global": {}}), "region"),
        (
            lambda e: e["projects"][PROJECT]["us-central1"].update(endpoint=[]),
            "endpoint",
        ),
        (
            lambda e: e["projects"][PROJECT]["us-central1"].update(models="x"),
            "models",
        ),
        (
            lambda e: e["projects"][PROJECT]["us-central1"]["endpoints"][0].update(
                deployedModels=5
            ),
            "deployedModels",
        ),
        (lambda e: e.update(owner_label_emails={"Claims Team": "x"}), "owner_label"),
    ],
)
def test_malformed_exports_are_refused(mutate, needle):
    export = fixture_export()
    mutate(export)
    imp.check_export(export)
    with pytest.raises(imp.ExportError, match=needle):
        imp.parse_gcp_export(export)


@pytest.mark.parametrize(
    "inject, path",
    [
        # A service account JSON key pasted into the export.
        (
            lambda e: e.update(
                service_account={"private_key_id": "k", "private_key": SECRET}
            ),
            "$.service_account.private_key",
        ),
        # `gcloud auth print-access-token --format=json` style output.
        (
            lambda e: e["projects"][PROJECT]["us-central1"]["endpoints"][0].update(
                access_token=SECRET
            ),
            f"$.projects.{PROJECT}.us-central1.endpoints[0].access_token",
        ),
        # An application default credentials file.
        (
            lambda e: e.update(adc={"client_id": "c", "refresh_token": SECRET}),
            "$.adc.refresh_token",
        ),
    ],
)
def test_export_with_a_credential_is_refused_without_echoing_it(inject, path):
    export = fixture_export()
    inject(export)
    with pytest.raises(imp.ExportError) as exc:
        imp.check_export(export)
    assert path in str(exc.value)
    assert SECRET not in str(exc.value)


# ── The export script, against a fake `gcloud` ─────────────────────────


def _fake_gcloud(tmp_path: Path, fixtures: Path = FIXTURES) -> tuple[Path, Path]:
    """A `gcloud` on PATH that replays the recorded output for
    `ai endpoints list` and `ai models list` only, and logs each call."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin" / "gcloud"
    fake.parent.mkdir()
    fake.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> {log}
[ "$1" = ai ] && [ "$3" = list ] || exit 9
case "$2" in endpoints|models) ;; *) exit 9;; esac
project=""; region=""
for arg in "$@"; do
  case "$arg" in
    --project=*) project="${{arg#--project=}}";;
    --region=*) region="${{arg#--region=}}";;
  esac
done
f={fixtures}/"$project.$region.$2.json"
if [ -f "$f" ]; then cat "$f"; else echo '[]'; fi
"""
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake.parent, log


needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")), reason="needs bash and jq"
)


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    bindir, _ = _fake_gcloud(tmp_path)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        check=False,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )


@needs_shell
def test_export_script_builds_an_importable_export_with_lists_only(tmp_path):
    out = _run(tmp_path, f"{PROJECT},acme-ml-dev", *REGIONS)
    assert out.returncode == 0, out.stderr
    export = json.loads(out.stdout)
    assert set(export["projects"]) == {PROJECT, "acme-ml-dev"}
    export["owner_label_emails"] = OWNER_EMAILS
    imp.check_export(export)
    _, deployments = imp.parse_gcp_export(export)
    assert _view(deployments) == EXPECTED
    calls = (tmp_path / "calls.log").read_text().splitlines()
    assert len(calls) == 2 * 2 * 2
    assert all(c.split()[0] == "ai" and c.split()[2] == "list" for c in calls)
    text = json.dumps(export)
    for dropped in ("containerSpec", "dedicatedResources", "trafficSplit", "etag"):
        assert dropped not in text


@needs_shell
def test_export_script_handles_listings_larger_than_an_argument(tmp_path):
    """Listings go through files: one argv string is capped at 128 KiB."""
    big = [
        {
            "name": f"projects/{PROJECT}/locations/us-central1/endpoints/{i}",
            "displayName": "x" * 100,
            "deployedModels": [],
        }
        for i in range(2000)
    ]
    data = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, data)
    (data / f"{PROJECT}.us-central1.endpoints.json").write_text(json.dumps(big))
    bindir, _ = _fake_gcloud(tmp_path, data)
    out = subprocess.run(
        ["bash", str(SCRIPT), PROJECT, *REGIONS],
        check=False,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr
    export = json.loads(out.stdout)
    assert len(export["projects"][PROJECT]["us-central1"]["endpoints"]) == 2000


@needs_shell
@pytest.mark.parametrize(
    "args",
    [
        ("Acme_Prod", "us-central1"),
        (PROJECT, "us-central1; rm -rf /"),
        (PROJECT,),
    ],
)
def test_export_script_refuses_bad_arguments(tmp_path, args):
    out = _run(tmp_path, *args)
    assert out.returncode == 2
    assert not (tmp_path / "calls.log").exists()


# ── Against PostgreSQL (PS_TEST_DATABASE_URL) ──────────────────────────


async def test_operator_route_imports_gcp_assets_idempotently(seeded_db, monkeypatch):
    monkeypatch.setenv("PS_ADMIN_TOKEN", OP_TOKEN)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()
    url = "/api/v1/admin/cloud-inventory/import"
    op = {"Authorization": f"Bearer {OP_TOKEN}"}
    body = {"tenant_id": str(tenant_id), "export": fixture_export()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(url, json=body, headers=op)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "tenant_id": str(tenant_id),
            "cloud": "gcp",
            "status": "ok",
            "projects": 1,
            "regions": 2,
            "deployments": len(EXPECTED),
            "assets_created": len(EXPECTED),
            "assets_updated": 0,
        }
        resp = await c.post(url, json=body, headers=op)
        assert resp.json()["assets_created"] == 0
        assert resp.json()["assets_updated"] == len(EXPECTED)
    async with factory() as db:
        assets = (
            (await db.execute(select(AIAsset).where(AIAsset.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        stored = {
            a.calling_service.removeprefix("gcp:"): (
                a.vendor,
                a.model,
                a.region,
                a.owner_email,
            )
            for a in assets
            if a.calling_service and a.calling_service.startswith("gcp:")
        }
        assert stored == {
            rid: ("google", model, region, owner)
            for rid, (model, region, _kind, owner) in EXPECTED.items()
        }


async def test_import_then_pull_update_the_same_gcp_assets(seeded_db):
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()
    now = datetime(2026, 11, 6, tzinfo=UTC)
    async with factory() as db:
        first = await imp.import_cloud_inventory(
            db, tenant_id, fixture_export(), now=now
        )
    assert first["assets_created"] == len(EXPECTED)
    settings = ci.CloudInventorySettings(
        cloud_inventory={str(tenant_id): ci.TenantCloudCredentials(gcp=_creds())}
    )
    async with factory() as db:
        pulled = await ci.pull_cloud_inventory(
            db,
            tenant_id,
            clouds=["gcp"],
            settings=settings,
            transport=httpx.MockTransport(_vertex_replay),
            sleep=_no_sleep,
            now=now,
        )
    assert pulled["clouds"]["gcp"]["assets_created"] == 0
    assert pulled["clouds"]["gcp"]["assets_updated"] == len(EXPECTED)
