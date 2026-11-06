"""Platform signal: import a cloud AI inventory export, with no cloud credentials.

``collector.cloud_inventory`` pulls deployments from the cloud control
plane with credentials the operator configures in ``PS_CLOUD_INVENTORY``.
Some organisations will not give the collector any cloud credential, even a
read-only one. For them, someone who already has read access runs the
cloud's own CLI (see ``docs/guides/discovery-sources.mdx``), and the JSON it
prints is imported here. The collector never sees, stores or logs a cloud
credential, and makes no cloud call.

Supported so far: AWS (Amazon Bedrock). The export is::

    {
      "cloud": "aws",
      "account_id": "123456789012",          # optional; checked against ARNs
      "regions": {
        "eu-central-1": {
          "custom_models":           <aws bedrock list-custom-models>,
          "imported_models":         <aws bedrock list-imported-models>,
          "provisioned_throughputs": <aws bedrock list-provisioned-model-throughputs>,
          "inference_profiles":      <aws bedrock list-inference-profiles
                                      --type-equals APPLICATION>,
          "tags": {"<resource ARN>": <aws bedrock list-tags-for-resource>}
        }
      }
    }

Each listing is the CLI's JSON output unchanged (or just its list of items);
any listing may be left out. The same rules as the live pull turn it into
assets (``cloud_inventory.bedrock_resources``), with the same asset ids, so
an import and a later pull update the same rows.

An export holding anything that looks like a credential (for example
``aws sts assume-role`` output pasted by mistake) is refused before it is
read, and the error names the field, never its value.

Run it with ``POST /api/v1/admin/cloud-inventory/import`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``, ``{"tenant_id": ..., "export":
{...}}``) or ``python -m collector.cloud_inventory_import --tenant <uuid>
--file export.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from db.models import Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from collector import cloud_inventory as ci
from collector import rls
from collector.provider_usage_admin import require_operator
from collector.sanitize import (
    UNSTORABLE_MESSAGE,
    BoundedJsonRoute,
    is_storable,
    loads_bounded,
)

logger = logging.getLogger("prompt_shields.cloud_inventory")

IMPORT_CLOUDS = ("aws",)
MAX_REGIONS = 40
MAX_ITEMS_PER_LISTING = 5000
MAX_TAGGED_RESOURCES = 20000
# Request body limit for the route, checked before the body is parsed (and
# before the operator token is, as for every JSON route).
MAX_BODY_BYTES = 32 * 1024 * 1024
_AWS_ACCOUNT = re.compile(r"^\d{12}$")

# Field names (lower case, letters and digits only) that hold credentials in
# AWS, Azure and GCP CLI output and config files. An export with a key that
# ends in one (``SecretAccessKey``, ``aws_secret_access_key``, ...) is refused.
CREDENTIAL_FIELDS = frozenset(
    {
        "accesskeyid",
        "secretaccesskey",
        "sessiontoken",
        "credentials",
        "clientsecret",
        "privatekey",
        "privatekeyid",
        "password",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "authorization",
    }
)


class ExportError(ValueError):
    """The export is malformed or holds a credential. The message names
    fields only, never values."""


def credential_fields(value: Any, path: str = "$") -> list[str]:
    """JSON paths of every object key in ``value`` that names a credential."""
    found: list[str] = []
    stack: list[tuple[Any, str]] = [(value, path)]
    while stack:
        node, here = stack.pop()
        if isinstance(node, dict):
            for key, child in node.items():
                where = f"{here}.{key}"
                name = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if any(name.endswith(f) for f in CREDENTIAL_FIELDS):
                    found.append(where)
                else:
                    stack.append((child, where))
        elif isinstance(node, list):
            stack.extend((child, f"{here}[{i}]") for i, child in enumerate(node))
    return sorted(found)


def _items(value: Any, key: str, what: str) -> list[dict]:
    """The items of a CLI listing: the output object (``{key: [...]}``),
    its bare list, or nothing."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get(key) or []
    if not isinstance(value, list):
        raise ExportError(f"{what}: expected the CLI output object or a list")
    if len(value) > MAX_ITEMS_PER_LISTING:
        raise ExportError(f"{what}: more than {MAX_ITEMS_PER_LISTING} items")
    return [item for item in value if isinstance(item, dict)]


def _tags(value: Any, arn: str) -> dict[str, str]:
    if isinstance(value, list):
        value = {"tags": value}
    if not isinstance(value, dict):
        raise ExportError(f"tags for {arn}: expected the CLI output object or a list")
    return ci.bedrock_tag_map(value)


class BedrockRegionExport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    custom_models: Any = None
    imported_models: Any = None
    provisioned_throughputs: Any = None
    inference_profiles: Any = None
    tags: dict[str, Any] = {}


class AwsInventoryExport(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    cloud: Literal["aws"]
    account_id: str | None = None
    regions: dict[str, BedrockRegionExport]

    @field_validator("account_id")
    @classmethod
    def _account(cls, v: str | None) -> str | None:
        if v is not None and not _AWS_ACCOUNT.match(v):
            raise ValueError("must be a 12-digit AWS account id")
        return v

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: dict[str, BedrockRegionExport]) -> dict:
        if not v:
            raise ValueError("at least one region is required")
        if len(v) > MAX_REGIONS:
            raise ValueError(f"at most {MAX_REGIONS} regions")
        bad = [r for r in v if not ci._AWS_REGION.match(r)]
        if bad:
            raise ValueError("invalid AWS region name")
        if sum(len(r.tags) for r in v.values()) > MAX_TAGGED_RESOURCES:
            raise ValueError(f"tags for more than {MAX_TAGGED_RESOURCES} resources")
        return v


def _validation_message(exc: ValidationError) -> str:
    # loc and msg only: pydantic's default text can quote the input.
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'export'}: {e['msg']}"
        for e in exc.errors(include_input=False, include_url=False)
    )


def parse_aws_export(export: dict) -> tuple[dict, list[ci.CloudDeployment]]:
    """(summary, deployments) from an AWS export. Raises ``ExportError``."""
    try:
        doc = AwsInventoryExport.model_validate(export)
    except ValidationError as exc:
        raise ExportError(_validation_message(exc)) from None
    deployments: list[ci.CloudDeployment] = []
    untagged = foreign = 0
    for region, r in doc.regions.items():
        found = ci.bedrock_resources(
            region,
            _items(r.custom_models, "modelSummaries", f"{region}.custom_models"),
            _items(r.imported_models, "modelSummaries", f"{region}.imported_models"),
            _items(
                r.provisioned_throughputs,
                "provisionedModelSummaries",
                f"{region}.provisioned_throughputs",
            ),
            _items(
                r.inference_profiles,
                "inferenceProfileSummaries",
                f"{region}.inference_profiles",
            ),
        )
        if doc.account_id:
            kept = [f for f in found if f[0].split(":")[4] == doc.account_id]
            foreign += len(found) - len(kept)
            found = kept
        tags = {arn: _tags(value, arn) for arn, value in r.tags.items()}
        untagged += sum(1 for arn, *_ in found if arn not in r.tags)
        deployments += ci.bedrock_deployments(region, found, tags)
    summary: dict = {"regions": len(doc.regions)}
    if untagged:
        # No tag listing in the export: owner, environment and business
        # unit stay empty for these until a pull or a later import.
        summary["resources_without_tags"] = untagged
    if foreign:
        summary["skipped_other_account"] = foreign
    return summary, deployments


def check_export(export: Any) -> str:
    """The export's cloud, after refusing credentials and unknown clouds."""
    if not isinstance(export, dict):
        raise ExportError("export must be a JSON object")
    if not is_storable(export):
        # Before the credential check: its message quotes key names.
        raise ExportError(f"export {UNSTORABLE_MESSAGE}")
    leaked = credential_fields(export)
    if leaked:
        raise ExportError(
            "export contains credential fields, refusing to read it: "
            + ", ".join(leaked[:10])
        )
    cloud = export.get("cloud")
    if cloud not in IMPORT_CLOUDS:
        raise ExportError(f"export.cloud must be one of: {', '.join(IMPORT_CLOUDS)}")
    return cloud


async def import_cloud_inventory(
    db: AsyncSession,
    tenant_id: UUID,
    export: Any,
    *,
    now: datetime | None = None,
) -> dict:
    """Store the assets in one export for one tenant and commit. Raises
    ``ExportError`` (nothing stored) for a malformed export."""
    cloud = check_export(export)
    summary, deployments = parse_aws_export(export)
    now = now or datetime.now(UTC)
    counts = await ci.store_inventory(db, tenant_id, deployments, now)
    await db.commit()
    logger.info(
        "cloud inventory import: tenant=%s cloud=%s %s", tenant_id, cloud, counts
    )
    return {
        "tenant_id": str(tenant_id),
        "cloud": cloud,
        "status": "ok",
        **summary,
        "deployments": len(deployments),
        **counts,
    }


class _SizeBoundedJsonRoute(BoundedJsonRoute):
    """``BoundedJsonRoute`` that first refuses a body over ``MAX_BODY_BYTES``
    with a 413, without reading more than that."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def size_bounded_handler(request: Request):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
                raise _too_large()
            chunks: list[bytes] = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BODY_BYTES:
                    raise _too_large()
                chunks.append(chunk)
            # Starlette's body cache: request.body() returns it.
            request._body = b"".join(chunks)
            return await handler(request)

        return size_bounded_handler


def _too_large() -> HTTPException:
    return _problem(413, "Payload Too Large", f"Body exceeds {MAX_BODY_BYTES} bytes")


router = APIRouter(
    prefix="/api/v1/admin", tags=["Admin"], route_class=_SizeBoundedJsonRoute
)


class CloudInventoryImportBody(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    tenant_id: UUID
    export: dict[str, Any]


def _problem(status: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
    )


@router.post(
    "/cloud-inventory/import",
    summary="Import a cloud AI inventory export, no cloud credentials (operator)",
    dependencies=[Depends(require_operator)],
)
async def import_cloud_inventory_endpoint(
    body: CloudInventoryImportBody, request: Request
) -> dict:
    try:
        check_export(body.export)
    except ExportError as exc:
        raise _problem(400, "Bad Request", str(exc)) from None
    db = request.state.db
    # One tenant per import: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
    if await db.get(Tenant, body.tenant_id) is None:
        raise _problem(404, "Not Found", "Tenant not found")
    try:
        return await import_cloud_inventory(db, body.tenant_id, body.export)
    except ExportError as exc:
        raise _problem(400, "Bad Request", str(exc)) from None


async def _main(tenant: UUID, export: Any) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            await rls.set_tenant(db, tenant)
            return await import_cloud_inventory(db, tenant, export)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument(
        "--file", required=True, help="the export JSON file, or - for stdin"
    )
    args = parser.parse_args(argv)
    if args.file == "-":
        raw = sys.stdin.read()
    else:
        with open(args.file, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        export = loads_bounded(raw)
        check_export(export)
    except ValueError as exc:
        # json errors quote a position, not content; ExportError names fields.
        parser.exit(2, f"error: {exc}\n")
    try:
        result = asyncio.run(_main(args.tenant, export))
    except ExportError as exc:
        parser.exit(2, f"error: {exc}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
