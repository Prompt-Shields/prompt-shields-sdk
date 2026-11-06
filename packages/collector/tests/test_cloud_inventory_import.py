"""Offline import of a cloud AI inventory export (#94), with no cloud
credentials. The export is built from recorded AWS CLI output in
tests/fixtures/cloud_inventory/aws; no cloud is called."""

from __future__ import annotations

import json
import logging
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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

FIXTURES = Path(__file__).parent / "fixtures" / "cloud_inventory" / "aws"
SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "cloud-inventory-export-aws.sh"
)
ACCOUNT = "123456789012"
REGIONS = ("eu-central-1", "us-east-1")
LISTINGS = {
    "custom_models": "list-custom-models",
    "imported_models": "list-imported-models",
    "provisioned_throughputs": "list-provisioned-model-throughputs",
    "inference_profiles": "list-inference-profiles",
}
EU = f"arn:aws:bedrock:eu-central-1:{ACCOUNT}"
CUSTOM = (
    f"{EU}:custom-model/anthropic.claude-3-haiku-20240307-v1:0:200k/claims-tuned-v2"
)
PT_EU = f"{EU}:provisioned-model/7t2kq9xw4m1z"
IMPORTED = f"{EU}:imported-model/k3x9q2m7v1ab"
PROFILE = f"{EU}:application-inference-profile/a1b2c3d4e5f6"
PT_US = f"arn:aws:bedrock:us-east-1:{ACCOUNT}:provisioned-model/9q8w7e6r5t4y"
OP_TOKEN = "op-token"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def fixture_export() -> dict:
    """The export the script builds from the recorded CLI output."""
    return {
        "cloud": "aws",
        "account_id": ACCOUNT,
        "regions": {
            r: {
                **{k: _load(f"{r}.{cmd}.json") for k, cmd in LISTINGS.items()},
                "tags": _load(f"{r}.tags.json"),
            }
            for r in REGIONS
        },
    }


EXPECTED = {
    # resource -> (model, region, kind, owner tag)
    CUSTOM: (
        "anthropic.claude-3-haiku-20240307-v1:0:200k",
        "eu-central-1",
        "custom",
        "claims-ai@acme.com",
    ),
    IMPORTED: (
        "llama-3-8b-underwriting",
        "eu-central-1",
        "imported",
        "underwriting-ml@acme.com",
    ),
    # The provisioned custom model inherits the custom model's tags.
    PT_EU: (
        "anthropic.claude-3-haiku-20240307-v1:0:200k",
        "eu-central-1",
        "provisioned",
        "claims-ai@acme.com",
    ),
    PROFILE: (
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        "eu-central-1",
        "inference_profile",
        "support-platform@acme.com",
    ),
    PT_US: (
        "amazon.titan-text-express-v1:0:8k",
        "us-east-1",
        "provisioned",
        "search@acme.com",
    ),
}


def _view(deployments: list[ci.CloudDeployment]) -> dict:
    return {
        d.resource_id: (d.model, d.region, d.model_format, ci.owner_from_tags(d.tags))
        for d in deployments
    }


def test_export_becomes_deployments_with_region_and_owner():
    summary, deployments = imp.parse_aws_export(fixture_export())
    # The custom model still training and the failed throughput are skipped.
    assert _view(deployments) == EXPECTED
    assert summary == {"regions": 2}
    pt = next(d for d in deployments if d.resource_id == PT_EU)
    assert pt.tags["business_unit"] == "Claims"
    assert pt.tags["cost-center"] == "cc-4410"


async def _no_sleep(_s: float) -> None:
    return None


def _bedrock_replay(request: httpx.Request) -> httpx.Response:
    """Serves the same recorded responses to the live pull."""
    region = request.url.host.split(".")[1]
    if request.url.path == "/listTagsForResource":
        arn = json.loads(request.content)["resourceARN"]
        return httpx.Response(
            200, json=_load(f"{region}.tags.json").get(arn, {"tags": []})
        )
    cmd = {
        "/custom-models": "list-custom-models",
        "/imported-models": "list-imported-models",
        "/provisioned-model-throughputs": "list-provisioned-model-throughputs",
        "/inference-profiles": "list-inference-profiles",
    }[request.url.path]
    return httpx.Response(200, json=_load(f"{region}.{cmd}.json"))


async def test_import_and_live_pull_agree_on_the_same_data():
    """Same rules and asset ids, so an import and a later pull converge."""
    creds = ci.AwsInventoryCredentials(
        access_key_id="AKIAEXAMPLEKEY12345",
        secret_access_key="s",
        regions=list(REGIONS),
    )
    _, pulled = await ci.fetch_aws_deployments(
        creds, transport=httpx.MockTransport(_bedrock_replay), sleep=_no_sleep
    )
    _, imported = imp.parse_aws_export(fixture_export())
    assert _view(pulled) == _view(imported) == EXPECTED
    tenant = uuid.uuid4()
    assert {ci.asset_id_for(tenant, d) for d in pulled} == {
        ci.asset_id_for(tenant, d) for d in imported
    }
    assert {d.resource_id: d.tags for d in pulled} == {
        d.resource_id: d.tags for d in imported
    }


def test_bare_lists_and_missing_listings_are_accepted():
    region = fixture_export()["regions"]["us-east-1"]
    export = {
        "cloud": "aws",
        "regions": {
            "us-east-1": {
                "provisioned_throughputs": region["provisioned_throughputs"][
                    "provisionedModelSummaries"
                ],
            }
        },
    }
    summary, deployments = imp.parse_aws_export(export)
    assert [(d.resource_id, d.tags) for d in deployments] == [(PT_US, {})]
    assert summary == {"regions": 1, "resources_without_tags": 1}


def test_resources_of_another_account_are_skipped():
    export = fixture_export()
    export["account_id"] = "210987654321"
    summary, deployments = imp.parse_aws_export(export)
    assert deployments == []
    assert summary["skipped_other_account"] == len(EXPECTED)


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda e: e.update(cloud="azure"), "export.cloud"),
        (lambda e: e.pop("cloud"), "export.cloud"),
        (lambda e: e.update(account_id="12345"), "account_id"),
        (lambda e: e["regions"].update({"mars-north-1": {}}), "region"),
        (lambda e: e.update(regions={}), "at least one region"),
        (lambda e: e["regions"]["us-east-1"].update(custom_modles=[]), "custom_modles"),
        (
            lambda e: e["regions"]["us-east-1"].update(custom_models="x"),
            "custom_models",
        ),
    ],
)
def test_malformed_exports_are_refused(mutate, needle):
    export = fixture_export()
    mutate(export)
    with pytest.raises(imp.ExportError, match=needle):
        imp.check_export(export)
        imp.parse_aws_export(export)


def test_listing_size_is_bounded(monkeypatch):
    monkeypatch.setattr(imp, "MAX_ITEMS_PER_LISTING", 1)
    with pytest.raises(imp.ExportError, match="more than 1 items"):
        imp.parse_aws_export(fixture_export())


SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


@pytest.mark.parametrize(
    "inject, path",
    [
        # `aws sts assume-role` output pasted into the export.
        (
            lambda e: e.update(Credentials={"SecretAccessKey": SECRET}),
            "$.Credentials",
        ),
        (
            lambda e: e["regions"]["eu-central-1"]["custom_models"].update(
                aws_secret_access_key=SECRET
            ),
            "$.regions.eu-central-1.custom_models.aws_secret_access_key",
        ),
        (
            lambda e: e["regions"]["us-east-1"]["provisioned_throughputs"][
                "provisionedModelSummaries"
            ][0].update(sessionToken=SECRET),
            (
                "$.regions.us-east-1.provisioned_throughputs."
                "provisionedModelSummaries[0].sessionToken"
            ),
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


def test_validation_errors_do_not_quote_the_input():
    export = fixture_export()
    export["account_id"] = SECRET
    with pytest.raises(imp.ExportError) as exc:
        imp.parse_aws_export(export)
    assert SECRET not in str(exc.value)


def _fake_aws(tmp_path: Path) -> tuple[Path, Path]:
    """An `aws` on PATH that replays the recorded output and logs each call."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin" / "aws"
    fake.parent.mkdir()
    fake.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> {log}
[ "$1" = bedrock ] || exit 9
cmd="$2"; shift 2
region=""; arn=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --region) region="$2"; shift 2;;
    --resource-arn) arn="$2"; shift 2;;
    *) shift;;
  esac
done
if [ "$cmd" = list-tags-for-resource ]; then
  jq --arg arn "$arn" '.[$arn] // {{"tags": []}}' {FIXTURES}/"$region".tags.json
else
  cat {FIXTURES}/"$region"."$cmd".json
fi
"""
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake.parent, log


@pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")), reason="needs bash and jq"
)
def test_export_script_builds_an_importable_export_with_reads_only(tmp_path):
    bindir, log = _fake_aws(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "AWS_ACCOUNT_ID": ACCOUNT,
    }
    out = subprocess.run(
        ["bash", str(SCRIPT), *REGIONS],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    export = json.loads(out.stdout)
    imp.check_export(export)
    _, deployments = imp.parse_aws_export(export)
    assert _view(deployments) == EXPECTED
    calls = log.read_text().splitlines()
    assert calls and all(c.split()[1].startswith("list-") for c in calls)
    assert any("--type-equals APPLICATION" in c for c in calls)


# ── Against PostgreSQL (PS_TEST_DATABASE_URL) ──────────────────────────


async def test_operator_route_imports_assets_for_the_target_tenant_only(
    seeded_db, monkeypatch, caplog
):
    monkeypatch.setenv("PS_ADMIN_TOKEN", OP_TOKEN)
    factory = async_sessionmaker(seeded_db, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        tenant_id = (await db.execute(select(Tenant.id))).scalar_one()
        other = Tenant(name="Other", domain="other.com", settings={})
        db.add(other)
        await db.commit()
        other_id = other.id

    url = "/api/v1/admin/cloud-inventory/import"
    op = {"Authorization": f"Bearer {OP_TOKEN}"}
    body = {"tenant_id": str(tenant_id), "export": fixture_export()}
    caplog.set_level(logging.DEBUG)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            url, json=body, headers={"Authorization": "Bearer ps-test-key-12345"}
        )
        assert resp.status_code == 401
        resp = await c.post(
            url, json={**body, "tenant_id": str(uuid.uuid4())}, headers=op
        )
        assert resp.status_code == 404

        leaky = fixture_export()
        leaky["Credentials"] = {"SecretAccessKey": SECRET}
        resp = await c.post(url, json={**body, "export": leaky}, headers=op)
        assert resp.status_code == 400
        assert "$.Credentials" in resp.json()["detail"]["detail"]
        assert SECRET not in resp.text

        resp = await c.post(url, json={**body, "export": {"cloud": "gcp"}}, headers=op)
        assert resp.status_code == 400

        resp = await c.post(url, json=body, headers=op)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "tenant_id": str(tenant_id),
            "cloud": "aws",
            "status": "ok",
            "regions": 2,
            "deployments": len(EXPECTED),
            "assets_created": len(EXPECTED),
            "assets_updated": 0,
        }
        # Re-importing updates the same rows.
        resp = await c.post(url, json=body, headers=op)
        assert resp.json()["assets_created"] == 0
        assert resp.json()["assets_updated"] == len(EXPECTED)
    assert SECRET not in caplog.text

    async with factory() as db:
        assets = (
            (await db.execute(select(AIAsset).where(AIAsset.tenant_id == tenant_id)))
            .scalars()
            .all()
        )
        stored = {
            a.calling_service.removeprefix("aws:"): (
                a.vendor,
                a.model,
                a.region,
                a.owner_email,
                a.discovery_source,
            )
            for a in assets
            if a.calling_service and a.calling_service.startswith("aws:")
        }
        assert stored == {
            arn: ("amazon", model, region, owner, ["platform_signal"])
            for arn, (model, region, _kind, owner) in EXPECTED.items()
        }
        envs = {a.calling_service: a.environment for a in assets}
        assert envs[f"aws:{IMPORTED}"] == "staging"
        assert envs[f"aws:{PROFILE}"] == "production"
        n_other = await db.scalar(
            select(func.count())
            .select_from(AIAsset)
            .where(AIAsset.tenant_id == other_id)
        )
        assert n_other == 0


async def test_import_then_pull_update_the_same_assets(seeded_db):
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
        cloud_inventory={
            str(tenant_id): ci.TenantCloudCredentials(
                aws=ci.AwsInventoryCredentials(
                    access_key_id="AKIAEXAMPLEKEY12345",
                    secret_access_key="s",
                    regions=list(REGIONS),
                )
            )
        }
    )
    async with factory() as db:
        pulled = await ci.pull_cloud_inventory(
            db,
            tenant_id,
            clouds=["aws"],
            settings=settings,
            transport=httpx.MockTransport(_bedrock_replay),
            sleep=_no_sleep,
            now=now,
        )
    assert pulled["clouds"]["aws"]["assets_created"] == 0
    assert pulled["clouds"]["aws"]["assets_updated"] == len(EXPECTED)
