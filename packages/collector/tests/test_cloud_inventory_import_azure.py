"""Offline import of an Azure AI inventory export (#94), with no cloud
credentials. The export is built from recorded `az` CLI output in
tests/fixtures/cloud_inventory/azure (shapes from the Azure CLI and ARM
reference docs); no cloud is called."""

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

from tests.test_cloud_inventory_import import OP_TOKEN, _no_sleep, _view

FIXTURES = Path(__file__).parent / "fixtures" / "cloud_inventory" / "azure"
SCRIPT = (
    Path(__file__).resolve().parents[3] / "scripts" / "cloud-inventory-export-azure.sh"
)
SUB = "3f2a8c1e-5b7d-4e2a-9c61-0d4e8f1a2b3c"
S = f"/subscriptions/{SUB}/resourceGroups"
ACC_WEU = f"{S}/rg-ai/providers/Microsoft.CognitiveServices/accounts/acme-openai-weu"
ACC_SEC = (
    f"{S}/rg-foundry/providers/Microsoft.CognitiveServices/accounts/acme-foundry-sec"
)
WS_CLAIMS = f"{S}/rg-ml/providers/Microsoft.MachineLearningServices/workspaces/claims-project"
WS_UW = f"{S}/rg-uw/providers/Microsoft.MachineLearningServices/workspaces/aml-underwriting"
WORKSPACES = {WS_CLAIMS: "claims-project", WS_UW: "aml-underwriting"}
ONLINE = {"claims-project": ["claims-triage"], "aml-underwriting": ["uw-risk"]}
SECRET = "azure-KEY-0123456789abcdef0123456789abcdef"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _ml(ws: str) -> dict:
    return {
        "serverless_endpoints": _load(f"ml.{ws}.serverless-endpoints.json"),
        "online_endpoints": _load(f"ml.{ws}.online-endpoints.json"),
        "online_deployments": {
            ep: _load(f"ml.{ws}.online-deployments.{ep}.json") for ep in ONLINE[ws]
        },
    }


def fixture_export() -> dict:
    """The recorded `az` output, unchanged, in the export layout."""
    return {
        "cloud": "azure",
        "subscriptions": {
            SUB: {
                "accounts": _load("accounts.json"),
                "deployments": {
                    ACC_WEU: _load("deployments.acme-openai-weu.json"),
                    ACC_SEC: _load("deployments.acme-foundry-sec.json"),
                },
                "ml_workspaces": _load("ml-workspaces.json"),
                "ml_endpoints": {ws_id: _ml(ws) for ws_id, ws in WORKSPACES.items()},
            }
        },
    }


EXPECTED = {
    # resource -> (model, region, kind, owner)
    f"{ACC_WEU}/deployments/claims-chat": (
        "gpt-4o",
        "westeurope",
        "OpenAI",
        "claims-ai@acme.com",
    ),
    f"{ACC_WEU}/deployments/claims-embeddings": (
        "text-embedding-3-large",
        "westeurope",
        "OpenAI",
        "claims-ai@acme.com",
    ),
    # The deployment's owner tag overrides the account's.
    f"{ACC_SEC}/deployments/support-assistant": (
        "Mistral-Large-2411",
        "swedencentral",
        "Mistral AI",
        "assistant-team@acme.com",
    ),
    f"{WS_CLAIMS}/serverlessEndpoints/claims-llama": (
        "Meta-Llama-3.1-70B-Instruct",
        "westeurope",
        "serverless",
        "claims-ai@acme.com",
    ),
    f"{WS_CLAIMS}/onlineEndpoints/claims-triage/deployments/blue": (
        "claims-triage-model",
        "westeurope",
        "online",
        "claims-ai@acme.com",
    ),
    f"{WS_UW}/onlineEndpoints/uw-risk/deployments/main": (
        "uw-risk-score",
        "northeurope",
        "online",
        "uw-risk@acme.com",
    ),
}


def test_export_becomes_deployments_with_region_and_owner():
    summary, deployments = imp.parse_azure_export(fixture_export())
    # Skipped: the language account, the failed deployment, the hub, the
    # failed serverless endpoint and the failed online deployment.
    assert _view(deployments) == EXPECTED
    assert summary == {"subscriptions": 1, "accounts": 2, "ml_workspaces": 2}
    by_id = {d.resource_id: d for d in deployments}
    chat = by_id[f"{ACC_WEU}/deployments/claims-chat"]
    assert chat.tags["cost-center"] == "cc-4410"
    assert ci.environment_from_tags(chat.tags) == "production"
    blue = by_id[f"{WS_CLAIMS}/onlineEndpoints/claims-triage/deployments/blue"]
    assert blue.tags["business_unit"] == "Claims Ops"


# ── The live pull, served the same recorded data as ARM responses ──────


def _arm_ml(item: dict) -> dict:
    """An `az ml` object in the ARM REST shape the live pull reads."""
    props: dict = {"provisioningState": item.get("provisioning_state")}
    if item.get("model_id"):
        props["modelSettings"] = {"modelId": item["model_id"]}
    if item.get("model"):
        props["model"] = item["model"]
    keep = {k: item[k] for k in ("id", "name", "location", "tags") if k in item}
    return {**keep, "properties": props}


def _arm_replay(request: httpx.Request) -> httpx.Response:
    if request.url.host == ci.AZURE_LOGIN_HOST:
        return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
    path = request.url.path
    if path == f"/subscriptions/{SUB}/providers/Microsoft.CognitiveServices/accounts":
        return httpx.Response(200, json={"value": _load("accounts.json")})
    for acc in (ACC_WEU, ACC_SEC):
        if path == f"{acc}/deployments":
            name = acc.rsplit("/", 1)[1]
            return httpx.Response(
                200, json={"value": _load(f"deployments.{name}.json")}
            )
    ws_list = f"/subscriptions/{SUB}/providers/Microsoft.MachineLearningServices/workspaces"
    if path == ws_list:
        return httpx.Response(200, json={"value": _load("ml-workspaces.json")})
    for ws_id, ws in WORKSPACES.items():
        rest = path.removeprefix(f"{ws_id}/")
        if rest == "serverlessEndpoints":
            items = _load(f"ml.{ws}.serverless-endpoints.json")
        elif rest == "onlineEndpoints":
            items = _load(f"ml.{ws}.online-endpoints.json")
        elif rest.startswith("onlineEndpoints/") and rest.endswith("/deployments"):
            ep = rest.split("/")[1]
            items = _load(f"ml.{ws}.online-deployments.{ep}.json")
        else:
            continue
        return httpx.Response(200, json={"value": [_arm_ml(i) for i in items]})
    return httpx.Response(404)


def _creds() -> ci.AzureInventoryCredentials:
    return ci.AzureInventoryCredentials(
        directory_id="acme.onmicrosoft.com",
        client_id="c",
        client_secret="s",
        subscription_ids=[SUB],
    )


async def test_import_and_live_pull_agree_on_the_same_data():
    """Same rules and asset ids, so an import and a later pull converge."""
    _, pulled = await ci.fetch_azure_deployments(
        _creds(), transport=httpx.MockTransport(_arm_replay), sleep=_no_sleep
    )
    _, imported = imp.parse_azure_export(fixture_export())
    assert _view(pulled) == _view(imported) == EXPECTED
    tenant = uuid.uuid4()
    assert {ci.asset_id_for(tenant, d) for d in pulled} == {
        ci.asset_id_for(tenant, d) for d in imported
    }
    assert {d.resource_id: d.tags for d in pulled} == {
        d.resource_id: d.tags for d in imported
    }


def test_arm_shaped_listings_are_accepted_too():
    """REST output (`{"value": [...]}`, ARM objects) imports the same."""
    export = fixture_export()
    sub = export["subscriptions"][SUB]
    sub["accounts"] = {"value": sub["accounts"]}
    for ml in sub["ml_endpoints"].values():
        ml["serverless_endpoints"] = {
            "value": [_arm_ml(i) for i in ml["serverless_endpoints"]]
        }
        ml["online_endpoints"] = [_arm_ml(i) for i in ml["online_endpoints"]]
        ml["online_deployments"] = {
            ep: [_arm_ml(i) for i in items]
            for ep, items in ml["online_deployments"].items()
        }
    _, deployments = imp.parse_azure_export(export)
    assert _view(deployments) == EXPECTED


def test_missing_listings_are_counted_not_guessed():
    export = fixture_export()
    sub = export["subscriptions"][SUB]
    del sub["deployments"][ACC_SEC]
    del sub["ml_endpoints"][WS_UW]
    sub["ml_endpoints"][WS_CLAIMS]["online_deployments"] = {}
    summary, deployments = imp.parse_azure_export(export)
    assert set(_view(deployments)) == {
        f"{ACC_WEU}/deployments/claims-chat",
        f"{ACC_WEU}/deployments/claims-embeddings",
        f"{WS_CLAIMS}/serverlessEndpoints/claims-llama",
    }
    assert summary == {
        "subscriptions": 1,
        "accounts": 2,
        "ml_workspaces": 2,
        "accounts_without_deployment_listing": 1,
        "ml_workspaces_without_endpoint_listing": 1,
        "online_endpoints_without_deployment_listing": 1,
    }


def test_resources_of_another_subscription_are_skipped():
    other = "00000000-1111-4222-8333-444444444444"
    export = fixture_export()
    export["subscriptions"] = {other: export["subscriptions"][SUB]}
    summary, deployments = imp.parse_azure_export(export)
    assert deployments == []
    assert summary["skipped_other_subscription"] == 4


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda e: e.update(subscriptions={}), "at least one subscription"),
        (lambda e: e.update(subscriptions={"sub-1": {}}), "GUIDs"),
        (lambda e: e["subscriptions"][SUB].update(acounts=[]), "acounts"),
        (lambda e: e["subscriptions"][SUB].update(accounts="x"), "accounts"),
        (
            lambda e: e["subscriptions"][SUB]["ml_endpoints"][WS_UW].update(
                online="x"
            ),
            "online",
        ),
        (lambda e: e.update(regions={}), "regions"),
    ],
)
def test_malformed_exports_are_refused(mutate, needle):
    export = fixture_export()
    mutate(export)
    imp.check_export(export)
    with pytest.raises(imp.ExportError, match=needle):
        imp.parse_azure_export(export)


@pytest.mark.parametrize(
    "inject, paths",
    [
        # `az account get-access-token` output pasted into the export.
        (
            lambda e: e.update(token={"accessToken": SECRET, "tokenType": "Bearer"}),
            ["$.token.accessToken"],
        ),
        # `az cognitiveservices account keys list` output.
        (
            lambda e: e["subscriptions"][SUB]["accounts"][0].update(
                keys={"key1": SECRET, "key2": SECRET}
            ),
            [
                f"$.subscriptions.{SUB}.accounts[0].keys.key1",
                f"$.subscriptions.{SUB}.accounts[0].keys.key2",
            ],
        ),
        # `az ml online-endpoint get-credentials` output.
        (
            lambda e: e["subscriptions"][SUB]["ml_endpoints"][WS_UW][
                "online_endpoints"
            ][0].update(primaryKey=SECRET, secondaryKey=SECRET),
            [
                (
                    f"$.subscriptions.{SUB}.ml_endpoints.{WS_UW}.online_endpoints[0]"
                    ".primaryKey"
                )
            ],
        ),
        # A service principal secret.
        (
            lambda e: e.update(principal={"clientSecret": SECRET}),
            ["$.principal.clientSecret"],
        ),
    ],
)
def test_export_with_a_credential_is_refused_without_echoing_it(inject, paths):
    export = fixture_export()
    inject(export)
    with pytest.raises(imp.ExportError) as exc:
        imp.check_export(export)
    for path in paths:
        assert path in str(exc.value)
    assert SECRET not in str(exc.value)


# ── The export script, against a fake `az` ─────────────────────────────

ALLOWED_AZ = (
    "account list",
    "extension list",
    "cognitiveservices account list",
    "cognitiveservices account deployment list",
    "resource list",
    "ml serverless-endpoint list",
    "ml online-endpoint list",
    "ml online-deployment list",
)


def _fake_az(tmp_path: Path) -> tuple[Path, Path]:
    """An `az` on PATH that replays the recorded output for list commands
    only (anything else exits 9) and logs each call."""
    log = tmp_path / "calls.log"
    fake = tmp_path / "bin" / "az"
    fake.parent.mkdir()
    fake.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
echo "$*" >> {log}
words=""
while [ "$#" -gt 0 ] && [ "${{1#--}}" = "$1" ]; do words="$words $1"; shift; done
words="${{words# }}"
name=""; ws=""; ep=""; sub=""; rtype=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2;;
    --workspace-name) ws="$2"; shift 2;;
    --endpoint-name) ep="$2"; shift 2;;
    --subscription) sub="$2"; shift 2;;
    --resource-type) rtype="$2"; shift 2;;
    *) shift;;
  esac
done
case "$words" in
  "account list") echo {SUB}; exit 0;;
  "extension list") echo '[{{"name": "ml", "version": "2.30.0"}}]'; exit 0;;
esac
[ "$sub" = {SUB} ] || exit 8
case "$words" in
  "cognitiveservices account list") cat {FIXTURES}/accounts.json;;
  "cognitiveservices account deployment list") cat {FIXTURES}/deployments."$name".json;;
  "resource list")
    [ "$rtype" = Microsoft.MachineLearningServices/workspaces ] || exit 7
    cat {FIXTURES}/ml-workspaces.json;;
  "ml serverless-endpoint list") cat {FIXTURES}/ml."$ws".serverless-endpoints.json;;
  "ml online-endpoint list") cat {FIXTURES}/ml."$ws".online-endpoints.json;;
  "ml online-deployment list") cat {FIXTURES}/ml."$ws".online-deployments."$ep".json;;
  *) exit 9;;
esac
"""
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return fake.parent, log


def _run_script(tmp_path: Path, *args: str, **env: str) -> tuple[dict, list[str]]:
    bindir, log = _fake_az(tmp_path)
    out = subprocess.run(
        ["bash", str(SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            **env,
        },
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(out.stdout), log.read_text().splitlines()


needs_shell = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("jq")), reason="needs bash and jq"
)


@needs_shell
def test_export_script_builds_an_importable_export_with_lists_only(tmp_path):
    export, calls = _run_script(tmp_path)
    imp.check_export(export)
    _, deployments = imp.parse_azure_export(export)
    assert _view(deployments) == EXPECTED
    # Only list commands, never keys or endpoint credentials.
    assert calls
    for call in calls:
        words = call.split(" --", 1)[0]
        assert words in ALLOWED_AZ, call
    assert not any("keys" in c or "credentials" in c or " show" in c for c in calls)
    # Only the fields the import reads leave the shell.
    text = json.dumps(export)
    for dropped in (
        "environment_variables",
        "AZUREML_ENTRY_SCRIPT",
        "scoring_uri",
        "endpoint\"",
        "principalId",
        "systemData",
    ):
        assert dropped not in text


@needs_shell
def test_export_script_can_leave_out_machine_learning(tmp_path):
    export, calls = _run_script(tmp_path, SUB, PS_EXPORT_SKIP_ML="1")
    assert not any(c.startswith(("ml ", "resource ", "extension ")) for c in calls)
    assert not any(c.startswith("account list") for c in calls)
    _, deployments = imp.parse_azure_export(export)
    assert {k for k in _view(deployments)} == {
        k for k in EXPECTED if "/Microsoft.CognitiveServices/" in k
    }


@needs_shell
def test_export_script_refuses_a_bad_subscription_id(tmp_path):
    bindir, log = _fake_az(tmp_path)
    out = subprocess.run(
        ["bash", str(SCRIPT), "not-a-guid; rm -rf /"],
        check=False,
        env={**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 2
    assert not log.exists() or "rm" not in log.read_text()


# ── Against PostgreSQL (PS_TEST_DATABASE_URL) ──────────────────────────


async def test_operator_route_imports_azure_assets_idempotently(
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
        leaky = fixture_export()
        leaky["subscriptions"][SUB]["accounts"][0]["keys"] = {"key1": SECRET}
        resp = await c.post(url, json={**body, "export": leaky}, headers=op)
        assert resp.status_code == 400
        assert "keys.key1" in resp.json()["detail"]["detail"]
        assert SECRET not in resp.text

        resp = await c.post(url, json=body, headers=op)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "tenant_id": str(tenant_id),
            "cloud": "azure",
            "status": "ok",
            "subscriptions": 1,
            "accounts": 2,
            "ml_workspaces": 2,
            "deployments": len(EXPECTED),
            "assets_created": len(EXPECTED),
            "assets_updated": 0,
        }
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
            a.calling_service.removeprefix("azure:"): (
                a.vendor,
                a.model,
                a.region,
                a.owner_email,
                a.discovery_source,
            )
            for a in assets
            if a.calling_service and a.calling_service.startswith("azure:")
        }
        assert stored == {
            rid: ("microsoft", model, region, owner, ["platform_signal"])
            for rid, (model, region, _kind, owner) in EXPECTED.items()
        }
        envs = {a.calling_service: a.environment for a in assets}
        assert envs[f"azure:{ACC_SEC}/deployments/support-assistant"] == "staging"
        assert envs[f"azure:{WS_UW}/onlineEndpoints/uw-risk/deployments/main"] == "dev"
        n_other = await db.scalar(
            select(func.count())
            .select_from(AIAsset)
            .where(AIAsset.tenant_id == other_id)
        )
        assert n_other == 0


async def test_import_then_pull_update_the_same_azure_assets(seeded_db):
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
        cloud_inventory={str(tenant_id): ci.TenantCloudCredentials(azure=_creds())}
    )
    async with factory() as db:
        pulled = await ci.pull_cloud_inventory(
            db,
            tenant_id,
            clouds=["azure"],
            settings=settings,
            transport=httpx.MockTransport(_arm_replay),
            sleep=_no_sleep,
            now=now,
        )
    assert pulled["clouds"]["azure"]["assets_created"] == 0
    assert pulled["clouds"]["azure"]["assets_updated"] == len(EXPECTED)
