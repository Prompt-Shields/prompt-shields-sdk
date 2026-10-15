"""Platform signal: AI model deployments inventoried from cloud accounts.

A model deployment in a cloud account is an AI asset whether or not any
traffic to it is instrumented. This module reads deployments, read-only,
from the cloud control plane and records each one as an ``AIAsset`` with
``discovery_source=["platform_signal"]`` (``medium`` confidence until
another source corroborates it), its ``region`` for sovereignty reporting,
and owner / environment / business unit taken from resource tags.

Supported so far:

  - Azure: Azure OpenAI and AI Foundry (``AIServices``) accounts and their
    model deployments, via Azure Resource Manager.
  - AWS: Amazon Bedrock provisioned throughput, custom (fine-tuned) models,
    imported models and application inference profiles, per configured
    region, via the Bedrock control plane (SigV4-signed, read-only).

Each asset's ``calling_service`` is ``azure:<ARM deployment resource id>``
or ``aws:<Bedrock resource ARN>``, which marks where it came from. Asset ids
are UUIDv5 values derived from the tenant, the cloud and that resource id,
so re-runs update the same rows.

Run it with ``POST /api/v1/admin/cloud-inventory/pull`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``, ``{"tenant_id": ...}``; 404 when
the token is unset) or ``python -m collector.cloud_inventory --tenant <uuid>``.

Credentials come from the operator's environment only, per tenant, in
``PS_CLOUD_INVENTORY`` (JSON, see ``CloudInventorySettings``); never from a
request body. Secrets are held as ``SecretStr`` and are never logged or
returned. HTTP calls go only to the fixed Microsoft hosts below, and to
``bedrock.<region>.amazonaws.com`` / ``sts.<region>.amazonaws.com`` for
strictly validated region names; ARM ``nextLink`` URLs, resource ids and
ARNs are checked before they are followed or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import re
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID

import httpx
from db.models import AIAsset, Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession

from collector.dedup import compute_confidence
from collector.lifecycle import reactivate
from collector.provider_usage_admin import require_operator

logger = logging.getLogger("prompt_shields.cloud_inventory")

SOURCE = "platform_signal"
SUPPORTED_CLOUDS = ("azure", "aws")

# Fixed hosts. Never derived from tenant input.
AZURE_LOGIN_HOST = "login.microsoftonline.com"
AZURE_ARM_HOST = "management.azure.com"
AZURE_ARM_SCOPE = "https://management.azure.com/.default"
ARM_SUBSCRIPTIONS_API = "2022-12-01"
ARM_COGNITIVE_API = "2024-10-01"
# Account kinds that host model deployments.
AZURE_AI_KINDS = {"openai", "aiservices"}
# Deployments in these states are not serving and are not inventoried.
INACTIVE_STATES = {"failed", "deleting", "deleted", "canceled"}

# AWS: hosts are built only from a validated region name.
AWS_BEDROCK_HOST = "bedrock.{region}.amazonaws.com"
AWS_STS_HOST = "sts.{region}.amazonaws.com"
STS_API_VERSION = "2011-06-15"
STS_SESSION_SECONDS = 900
# Commercial and GovCloud regions (the amazonaws.com partitions), e.g.
# "eu-central-1", "us-gov-west-1", "ap-southeast-4".
_AWS_REGION = re.compile(
    r"^(?:us|eu|ap|ca|sa|me|af|il|mx)(?:-gov)?-"
    r"(?:north|south|east|west|central|northeast|northwest|southeast|southwest)"
    r"-[1-9]$"
)
_AWS_ACCESS_KEY = re.compile(r"^[A-Z0-9]{16,128}$")
_AWS_ROLE_ARN = re.compile(r"^arn:aws(?:-us-gov)?:iam::\d{12}:role/[\w+=,.@/-]{1,512}$")
_AWS_EXTERNAL_ID = re.compile(r"^[\w+=,.@:/-]{2,1224}$")
_AWS_SESSION_NAME = re.compile(r"^[\w+=,.@-]{2,64}$")
# Bedrock resources inventoried, keyed by ARN resource type.
_BEDROCK_ARN = re.compile(
    r"^arn:aws(?:-us-gov)?:bedrock:(?P<region>[a-z0-9-]+):\d{12}:"
    r"(?P<type>custom-model|provisioned-model|imported-model"
    r"|application-inference-profile)/[A-Za-z0-9._:/+-]{1,1011}$"
)
_FOUNDATION_MODEL_ARN = re.compile(
    r"^arn:aws(?:-us-gov)?:bedrock:[a-z0-9-]+:\d{0,12}:"
    r"(?:foundation-model|inference-profile)/(?P<id>[A-Za-z0-9._:-]{1,256})$"
)
# Bedrock states that are not (or no longer) usable.
AWS_INACTIVE_STATES = {"failed", "deleting", "deleted"}

MAX_PAGES = 200
MAX_RETRIES = 4
MAX_RETRY_WAIT_S = 60.0

_NAMESPACE = uuid.UUID("2c0f7a91-5b3e-5d8c-8e41-7a6b9d2f0c13")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_ACCOUNT_ID = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/"
    r"Microsoft\.CognitiveServices/accounts/[^/]+$",
    re.IGNORECASE,
)

OWNER_TAGS = ("owner", "owner_email", "owneremail", "owner-email", "contact")
ENVIRONMENT_TAGS = ("environment", "env")
BUSINESS_UNIT_TAGS = ("business_unit", "businessunit", "business-unit", "department")
_ENVIRONMENTS = {
    "production": "production",
    "prod": "production",
    "prd": "production",
    "staging": "staging",
    "stage": "staging",
    "stg": "staging",
    "dev": "dev",
    "development": "dev",
    "test": "dev",
}


class AzureInventoryCredentials(BaseModel):
    """A read-only service principal (``Reader`` role, see the guide)."""

    directory_id: str  # Microsoft Entra tenant id or domain
    client_id: str
    client_secret: SecretStr
    # Empty: every subscription the principal can read.
    subscription_ids: list[str] = []

    @field_validator("directory_id")
    @classmethod
    def _directory(cls, v: str) -> str:
        if not _DIRECTORY.match(v):
            raise ValueError("directory_id must be a tenant id or domain")
        return v

    @field_validator("subscription_ids")
    @classmethod
    def _subscriptions(cls, v: list[str]) -> list[str]:
        if not all(_GUID.match(s) for s in v):
            raise ValueError("subscription_ids must be GUIDs")
        return v


class AwsInventoryCredentials(BaseModel):
    """A read-only IAM principal (minimal policy in the guide).

    Static access keys, used directly or, with ``role_arn``, only to call
    ``sts:AssumeRole`` (with ``external_id`` when the role requires one) for
    short-lived credentials in the target account. Nothing is read from the
    process's own AWS environment, profile files or instance metadata.
    """

    access_key_id: str
    secret_access_key: SecretStr
    session_token: SecretStr | None = None
    role_arn: str | None = None
    external_id: SecretStr | None = None
    role_session_name: str = "prompt-shields-inventory"
    # Bedrock is regional: every region to inventory must be listed.
    regions: list[str]
    # Also report whether model invocation logging is enabled per region.
    check_invocation_logging: bool = False

    @field_validator("access_key_id")
    @classmethod
    def _access_key(cls, v: str) -> str:
        if not _AWS_ACCESS_KEY.match(v):
            raise ValueError("access_key_id is malformed")
        return v

    @field_validator("role_arn")
    @classmethod
    def _role(cls, v: str | None) -> str | None:
        if v is not None and not _AWS_ROLE_ARN.match(v):
            raise ValueError("role_arn must be an IAM role ARN")
        return v

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, v: SecretStr | None) -> SecretStr | None:
        if v is not None and not _AWS_EXTERNAL_ID.match(v.get_secret_value()):
            raise ValueError("external_id is malformed")
        return v

    @field_validator("role_session_name")
    @classmethod
    def _session_name(cls, v: str) -> str:
        if not _AWS_SESSION_NAME.match(v):
            raise ValueError("role_session_name is malformed")
        return v

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        if not v or len(v) > 40 or not all(_AWS_REGION.match(r) for r in v):
            raise ValueError("regions must be 1-40 AWS region names")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _external_id_needs_role(self):
        if self.external_id is not None and self.role_arn is None:
            raise ValueError("external_id requires role_arn")
        return self


class TenantCloudCredentials(BaseModel):
    azure: AzureInventoryCredentials | None = None
    aws: AwsInventoryCredentials | None = None


class CloudInventorySettings(BaseSettings):
    """``PS_CLOUD_INVENTORY='{"<tenant uuid>": {"azure": {"directory_id": ...,
    "client_id": ..., "client_secret": ..., "subscription_ids": [...]},
    "aws": {"access_key_id": ..., "secret_access_key": ..., "role_arn": ...,
    "external_id": ..., "regions": [...]}}}'``"""

    cloud_inventory: dict[str, TenantCloudCredentials] = {}

    # A malformed value must not echo the client secret in the error.
    model_config = {
        "env_prefix": "PS_",
        "extra": "ignore",
        "hide_input_in_errors": True,
    }


def credentials_for_tenant(
    tenant_id: UUID, settings: CloudInventorySettings | None = None
) -> TenantCloudCredentials:
    settings = settings or CloudInventorySettings()
    return settings.cloud_inventory.get(str(tenant_id)) or TenantCloudCredentials()


class CloudInventoryError(Exception):
    """A cloud call failed. The message never contains credentials."""


@dataclass(frozen=True)
class CloudDeployment:
    cloud: str
    resource_id: str
    region: str | None
    model: str | None
    model_format: str | None
    tags: dict[str, str] = field(default_factory=dict)


Sleep = Callable[[float], Awaitable[None]]


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_WAIT_S))
    except ValueError:
        return None


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    what: str,
    sleep: Sleep,
    cloud: str = "azure",
    as_text: bool = False,
    sign: Callable[[], dict[str, str]] | None = None,
    **kwargs,
):
    """Request with retry on 429/5xx, honouring Retry-After. ``sign``, when
    given, builds fresh headers for every attempt."""
    for attempt in range(MAX_RETRIES + 1):
        if sign is not None:
            kwargs["headers"] = sign()
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.TransportError as exc:
            if attempt == MAX_RETRIES:
                raise CloudInventoryError(
                    f"{cloud}: {what}: transport error ({type(exc).__name__})"
                )
            await sleep(min(2.0**attempt, MAX_RETRY_WAIT_S))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                break
            await sleep(_retry_after(resp) or min(2.0**attempt, MAX_RETRY_WAIT_S))
            continue
        if resp.status_code != 200:
            # The body is not echoed: token errors can quote the request.
            raise CloudInventoryError(
                f"{cloud}: {what} returned HTTP {resp.status_code}"
            )
        if as_text:
            return resp.text
        try:
            return resp.json()
        except ValueError:
            raise CloudInventoryError(f"{cloud}: {what} returned invalid JSON")
    raise CloudInventoryError(
        f"{cloud}: {what} still failing after {MAX_RETRIES} retries"
    )


def _arm_url(next_link: str) -> str:
    """Only follow ``nextLink`` back to ARM over https, so the bearer token
    is never sent to another host."""
    url = httpx.URL(next_link)
    if url.scheme != "https" or url.host != AZURE_ARM_HOST:
        raise CloudInventoryError("azure: unexpected nextLink host")
    return str(url)


async def _arm_list(
    client: httpx.AsyncClient,
    path: str,
    api_version: str,
    headers: dict[str, str],
    what: str,
    sleep: Sleep,
):
    url: str | None = f"https://{AZURE_ARM_HOST}{path}"
    params: dict | None = {"api-version": api_version}
    for _ in range(MAX_PAGES):
        body = await _request(
            client, "GET", url, what=what, sleep=sleep, params=params, headers=headers
        )
        for item in body.get("value") or []:
            yield item
        next_link = body.get("nextLink")
        if not next_link:
            return
        url, params = _arm_url(next_link), None
    raise CloudInventoryError(f"azure: {what}: more than {MAX_PAGES} pages")


async def _azure_token(
    client: httpx.AsyncClient, creds: AzureInventoryCredentials, sleep: Sleep
) -> str:
    body = await _request(
        client,
        "POST",
        f"https://{AZURE_LOGIN_HOST}/{creds.directory_id}/oauth2/v2.0/token",
        what="token request",
        sleep=sleep,
        data={
            "grant_type": "client_credentials",
            "client_id": creds.client_id,
            "client_secret": creds.client_secret.get_secret_value(),
            "scope": AZURE_ARM_SCOPE,
        },
    )
    token = body.get("access_token")
    if not isinstance(token, str) or not token:
        raise CloudInventoryError("azure: token response had no access_token")
    return token


def _tags(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if v is not None}


async def fetch_azure_deployments(
    creds: AzureInventoryCredentials,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
) -> tuple[int, list[CloudDeployment]]:
    """Return (AI accounts seen, model deployments) for one service principal."""
    deployments: list[CloudDeployment] = []
    accounts_seen = 0
    async with httpx.AsyncClient(
        transport=transport, timeout=30.0, follow_redirects=False
    ) as client:
        headers = {
            "Authorization": f"Bearer {await _azure_token(client, creds, sleep)}"
        }
        subscriptions = list(creds.subscription_ids)
        if not subscriptions:
            async for sub in _arm_list(
                client,
                "/subscriptions",
                ARM_SUBSCRIPTIONS_API,
                headers,
                "subscription list",
                sleep,
            ):
                sid = sub.get("subscriptionId")
                if isinstance(sid, str) and _GUID.match(sid):
                    subscriptions.append(sid)

        for sid in subscriptions:
            async for account in _arm_list(
                client,
                f"/subscriptions/{sid}/providers/Microsoft.CognitiveServices/accounts",
                ARM_COGNITIVE_API,
                headers,
                "account list",
                sleep,
            ):
                if str(account.get("kind", "")).lower() not in AZURE_AI_KINDS:
                    continue
                account_id = account.get("id")
                if not isinstance(account_id, str) or not _ACCOUNT_ID.match(account_id):
                    logger.warning("azure: skipping account with unexpected id")
                    continue
                accounts_seen += 1
                region = account.get("location")
                account_tags = _tags(account.get("tags"))
                async for dep in _arm_list(
                    client,
                    f"{account_id}/deployments",
                    ARM_COGNITIVE_API,
                    headers,
                    "deployment list",
                    sleep,
                ):
                    props = dep.get("properties") or {}
                    state = str(props.get("provisioningState") or "").lower()
                    name = dep.get("name")
                    if (
                        state in INACTIVE_STATES
                        or not isinstance(name, str)
                        or not name
                    ):
                        continue
                    model = props.get("model") or {}
                    deployments.append(
                        CloudDeployment(
                            cloud="azure",
                            resource_id=f"{account_id}/deployments/{name}",
                            region=(region or "").lower() or None,
                            model=model.get("name"),
                            model_format=model.get("format"),
                            # Deployment tags override the account's.
                            tags={**account_tags, **_tags(dep.get("tags"))},
                        )
                    )
    return accounts_seen, deployments


# --- AWS Bedrock -----------------------------------------------------------


@dataclass(frozen=True)
class AwsKeys:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    session_token: str | None = field(default=None, repr=False)


def _aws_quote(value: str) -> str:
    # SigV4 URI encoding: everything but RFC 3986 unreserved characters.
    return quote(value, safe="-_.~")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def canonical_query(params: dict[str, str]) -> str:
    return "&".join(
        f"{_aws_quote(k)}={_aws_quote(v)}"
        for k, v in sorted((str(k), str(v)) for k, v in params.items())
    )


def sigv4_headers(
    method: str,
    host: str,
    path: str,
    query: str,
    body: bytes,
    *,
    region: str,
    service: str,
    keys: AwsKeys,
    now: datetime,
    content_type: str | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 headers for one request.

    ``query`` must already be canonical (``canonical_query``) and is sent
    exactly as signed; ``path`` must not need encoding. The returned headers
    include ``host`` because it is signed.
    """
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    headers = {"host": host, "x-amz-date": amz_date}
    if content_type:
        headers["content-type"] = content_type
    if keys.session_token:
        headers["x-amz-security-token"] = keys.session_token
    signed = sorted(headers)
    canonical_request = "\n".join(
        [
            method,
            path,
            query,
            "".join(f"{h}:{' '.join(headers[h].split())}\n" for h in signed),
            ";".join(signed),
            _sha256(body),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical_request.encode())]
    )
    key = _hmac(("AWS4" + keys.secret_access_key).encode(), day)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={keys.access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(signed)}, Signature={signature}"
    )
    return headers


Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _aws_call(
    client: httpx.AsyncClient,
    method: str,
    host: str,
    path: str,
    *,
    region: str,
    service: str,
    keys: AwsKeys,
    what: str,
    sleep: Sleep,
    clock: Clock,
    params: dict[str, str] | None = None,
    body: bytes = b"",
    content_type: str | None = None,
    as_text: bool = False,
):
    query = canonical_query(params or {})

    def sign() -> dict[str, str]:
        # Each attempt is signed afresh, so retries after a long backoff stay
        # inside the five-minute SigV4 clock-skew window.
        return sigv4_headers(
            method,
            host,
            path,
            query,
            body,
            region=region,
            service=service,
            keys=keys,
            now=clock(),
            content_type=content_type,
        )

    url = f"https://{host}{path}" + (f"?{query}" if query else "")
    return await _request(
        client,
        method,
        url,
        what=what,
        sleep=sleep,
        cloud="aws",
        as_text=as_text,
        sign=sign,
        content=body,
    )


def _xml_text(root: ET.Element, name: str) -> str | None:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == name and el.text:
            return el.text.strip()
    return None


async def _aws_assume_role(
    client: httpx.AsyncClient,
    creds: AwsInventoryCredentials,
    base: AwsKeys,
    sleep: Sleep,
    clock: Clock,
) -> AwsKeys:
    """Short-lived credentials for ``creds.role_arn`` from the regional STS
    endpoint of the first configured region."""
    region = creds.regions[0]
    form = {
        "Action": "AssumeRole",
        "Version": STS_API_VERSION,
        "RoleArn": creds.role_arn,
        "RoleSessionName": creds.role_session_name,
        "DurationSeconds": str(STS_SESSION_SECONDS),
    }
    if creds.external_id is not None:
        form["ExternalId"] = creds.external_id.get_secret_value()
    text = await _aws_call(
        client,
        "POST",
        AWS_STS_HOST.format(region=region),
        "/",
        region=region,
        service="sts",
        keys=base,
        what="assume role",
        sleep=sleep,
        clock=clock,
        body=canonical_query(form).encode(),
        content_type="application/x-www-form-urlencoded; charset=utf-8",
        as_text=True,
    )
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise CloudInventoryError("aws: assume role returned invalid XML")
    key_id = _xml_text(root, "AccessKeyId")
    secret = _xml_text(root, "SecretAccessKey")
    token = _xml_text(root, "SessionToken")
    if not (key_id and secret and token):
        raise CloudInventoryError("aws: assume role response had no credentials")
    return AwsKeys(key_id, secret, token)


async def _bedrock_list(
    client: httpx.AsyncClient,
    region: str,
    path: str,
    items_key: str,
    keys: AwsKeys,
    what: str,
    sleep: Sleep,
    clock: Clock,
    params: dict[str, str] | None = None,
):
    host = AWS_BEDROCK_HOST.format(region=region)
    token: str | None = None
    for _ in range(MAX_PAGES):
        page = {**(params or {}), "maxResults": "100"}
        if token:
            page["nextToken"] = token
        body = await _aws_call(
            client,
            "GET",
            host,
            path,
            region=region,
            service="bedrock",
            keys=keys,
            what=f"{what} ({region})",
            sleep=sleep,
            clock=clock,
            params=page,
        )
        for item in body.get(items_key) or []:
            if isinstance(item, dict):
                yield item
        token = body.get("nextToken")
        if not isinstance(token, str) or not token:
            return
    raise CloudInventoryError(f"aws: {what} ({region}): more than {MAX_PAGES} pages")


async def _bedrock_tags(
    client: httpx.AsyncClient,
    region: str,
    arn: str,
    keys: AwsKeys,
    sleep: Sleep,
    clock: Clock,
) -> dict[str, str]:
    # ListTagsForResource is a read, sent as POST with a JSON body.
    body = await _aws_call(
        client,
        "POST",
        AWS_BEDROCK_HOST.format(region=region),
        "/listTagsForResource",
        region=region,
        service="bedrock",
        keys=keys,
        what=f"tag list ({region})",
        sleep=sleep,
        clock=clock,
        body=json.dumps({"resourceARN": arn}).encode(),
        content_type="application/json",
    )
    return {
        str(t["key"]): str(t["value"])
        for t in body.get("tags") or []
        if isinstance(t, dict) and t.get("key") and t.get("value") is not None
    }


def _bedrock_arn(value, region: str, kind: str) -> str | None:
    """The ARN if it is a well-formed Bedrock ARN of ``kind`` in ``region``."""
    if not isinstance(value, str):
        return None
    m = _BEDROCK_ARN.match(value)
    if not m or m["region"] != region or m["type"] != kind:
        return None
    return value


def _model_id(arn) -> str | None:
    """``anthropic.claude-3-haiku-...`` from a foundation model or
    system-defined inference profile ARN."""
    if not isinstance(arn, str):
        return None
    m = _FOUNDATION_MODEL_ARN.match(arn)
    return m["id"] if m else None


def _active(item: dict, status_key: str) -> bool:
    return str(item.get(status_key) or "").lower() not in AWS_INACTIVE_STATES


async def _bedrock_region(
    client: httpx.AsyncClient,
    region: str,
    keys: AwsKeys,
    sleep: Sleep,
    clock: Clock,
) -> list[CloudDeployment]:
    """Bedrock resources in one region that represent AI in use."""
    # (arn, model, kind, parent arn whose tags it inherits)
    found: list[tuple[str, str | None, str, str | None]] = []

    # Custom (fine-tuned, continued pre-training, distilled) models.
    custom_models: dict[str, str | None] = {}
    async for m in _bedrock_list(
        client,
        region,
        "/custom-models",
        "modelSummaries",
        keys,
        "custom model list",
        sleep,
        clock,
    ):
        arn = _bedrock_arn(m.get("modelArn"), region, "custom-model")
        if arn is None or not _active(m, "modelStatus"):
            continue
        model = _model_id(m.get("baseModelArn")) or m.get("baseModelName")
        custom_models[arn] = model
        found.append((arn, model, "custom", None))

    # Imported (bring-your-own-weights) models.
    async for m in _bedrock_list(
        client,
        region,
        "/imported-models",
        "modelSummaries",
        keys,
        "imported model list",
        sleep,
        clock,
    ):
        arn = _bedrock_arn(m.get("modelArn"), region, "imported-model")
        if arn is None:
            continue
        name = m.get("modelName")
        found.append((arn, name if isinstance(name, str) else None, "imported", None))

    # Provisioned throughput: dedicated, paid serving capacity.
    async for p in _bedrock_list(
        client,
        region,
        "/provisioned-model-throughputs",
        "provisionedModelSummaries",
        keys,
        "provisioned throughput list",
        sleep,
        clock,
    ):
        arn = _bedrock_arn(p.get("provisionedModelArn"), region, "provisioned-model")
        if arn is None or not _active(p, "status"):
            continue
        parent = p.get("modelArn") if p.get("modelArn") in custom_models else None
        model = _model_id(p.get("foundationModelArn")) or custom_models.get(parent)
        found.append((arn, model, "provisioned", parent))

    # Application inference profiles: created to route and meter usage.
    async for p in _bedrock_list(
        client,
        region,
        "/inference-profiles",
        "inferenceProfileSummaries",
        keys,
        "inference profile list",
        sleep,
        clock,
        params={"type": "APPLICATION"},
    ):
        arn = _bedrock_arn(
            p.get("inferenceProfileArn"), region, "application-inference-profile"
        )
        if arn is None or not _active(p, "status"):
            continue
        models = [
            m.get("modelArn") for m in p.get("models") or [] if isinstance(m, dict)
        ]
        model = next((i for i in map(_model_id, models) if i), None)
        found.append((arn, model, "inference_profile", None))

    tags_by_arn: dict[str, dict[str, str]] = {}
    for arn, *_ in found:
        tags_by_arn[arn] = await _bedrock_tags(client, region, arn, keys, sleep, clock)
    return [
        CloudDeployment(
            cloud="aws",
            resource_id=arn,
            region=region,
            model=model,
            model_format=kind,
            # A provisioned custom model's own tags override the model's.
            tags={**tags_by_arn.get(parent or "", {}), **tags_by_arn[arn]},
        )
        for arn, model, kind, parent in found
    ]


async def _bedrock_logging_enabled(
    client: httpx.AsyncClient,
    region: str,
    keys: AwsKeys,
    sleep: Sleep,
    clock: Clock,
) -> bool:
    body = await _aws_call(
        client,
        "GET",
        AWS_BEDROCK_HOST.format(region=region),
        "/logging/modelinvocations",
        region=region,
        service="bedrock",
        keys=keys,
        what=f"invocation logging config ({region})",
        sleep=sleep,
        clock=clock,
    )
    config = body.get("loggingConfig") or {}
    return bool(config.get("cloudWatchConfig") or config.get("s3Config"))


async def fetch_aws_deployments(
    creds: AwsInventoryCredentials,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = _utcnow,
) -> tuple[dict, list[CloudDeployment]]:
    """Return (summary, Bedrock resources) for one IAM principal.

    A failing region is reported in ``summary["region_errors"]`` and does
    not stop the others. Failing to assume the role, or every region
    failing, raises ``CloudInventoryError``.
    """
    keys = AwsKeys(
        creds.access_key_id,
        creds.secret_access_key.get_secret_value(),
        creds.session_token.get_secret_value() if creds.session_token else None,
    )
    deployments: list[CloudDeployment] = []
    errors: dict[str, str] = {}
    logging_enabled: dict[str, bool] = {}
    async with httpx.AsyncClient(
        transport=transport, timeout=30.0, follow_redirects=False
    ) as client:
        if creds.role_arn:
            keys = await _aws_assume_role(client, creds, keys, sleep, clock)
        for region in creds.regions:
            try:
                deployments += await _bedrock_region(client, region, keys, sleep, clock)
                if creds.check_invocation_logging:
                    logging_enabled[region] = await _bedrock_logging_enabled(
                        client, region, keys, sleep, clock
                    )
            except CloudInventoryError as exc:
                errors[region] = str(exc)
    if len(errors) == len(creds.regions):
        raise CloudInventoryError("; ".join(errors.values()))
    summary: dict = {"regions": len(creds.regions)}
    if errors:
        summary["region_errors"] = errors
    if creds.check_invocation_logging:
        summary["invocation_logging"] = logging_enabled
    return summary, deployments


def _tag(tags: dict[str, str], names: tuple[str, ...]) -> str | None:
    lowered = {k.lower(): v.strip() for k, v in tags.items()}
    for n in names:
        if lowered.get(n):
            return lowered[n]
    return None


def owner_from_tags(tags: dict[str, str]) -> str | None:
    owner = _tag(tags, OWNER_TAGS)
    return owner if owner and "@" in owner else None


def environment_from_tags(tags: dict[str, str]) -> str | None:
    env = _tag(tags, ENVIRONMENT_TAGS)
    return _ENVIRONMENTS.get(env.lower()) if env else None


def vendor_for(d: CloudDeployment) -> str:
    # The cloud provider is the supplier (and processor) of a hosted model.
    return {"azure": "microsoft", "aws": "amazon"}[d.cloud]


def asset_id_for(tenant_id: UUID, d: CloudDeployment) -> UUID:
    return uuid.uuid5(_NAMESPACE, f"{tenant_id}|{d.cloud}|{d.resource_id.lower()}")


async def store_inventory(
    db: AsyncSession,
    tenant_id: UUID,
    deployments: list[CloudDeployment],
    now: datetime,
) -> dict:
    """Upsert one asset per deployment. Does not commit.

    The cloud is authoritative for model and region. Owner, environment and
    business unit are filled from tags only while empty, so values curated
    in the registry are not overwritten.
    """
    created = updated = 0
    for d in deployments:
        aid = asset_id_for(tenant_id, d)
        owner = owner_from_tags(d.tags)
        environment = environment_from_tags(d.tags)
        business_unit = _tag(d.tags, BUSINESS_UNIT_TAGS)
        asset = await db.get(AIAsset, aid)
        if asset is None:
            db.add(
                AIAsset(
                    id=aid,
                    tenant_id=tenant_id,
                    vendor=vendor_for(d),
                    model=d.model,
                    region=d.region,
                    owner_email=owner,
                    environment=environment,
                    business_unit=business_unit,
                    calling_service=f"{d.cloud}:{d.resource_id}",
                    status="active",
                    discovery_source=[SOURCE],
                    confidence=compute_confidence([SOURCE]),
                    first_seen=now,
                    last_seen=now,
                )
            )
            created += 1
            continue
        asset.model = d.model
        asset.region = d.region
        asset.owner_email = asset.owner_email or owner
        asset.environment = asset.environment or environment
        asset.business_unit = asset.business_unit or business_unit
        if SOURCE not in (asset.discovery_source or []):
            asset.discovery_source = list(asset.discovery_source or []) + [SOURCE]
            asset.confidence = compute_confidence(asset.discovery_source)
        if asset.last_seen is None or now > asset.last_seen:
            asset.last_seen = now
        reactivate(asset, now)
        updated += 1
    await db.flush()
    return {"assets_created": created, "assets_updated": updated}


async def pull_cloud_inventory(
    db: AsyncSession,
    tenant_id: UUID,
    *,
    clouds: list[str] | None = None,
    settings: CloudInventorySettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    """Inventory every configured cloud for one tenant and commit.

    A failing cloud is reported in the result and does not stop the others.
    """
    now = now or datetime.now(UTC)
    creds = credentials_for_tenant(tenant_id, settings)
    wanted = [c for c in (clouds or SUPPORTED_CLOUDS) if c in SUPPORTED_CLOUDS]
    result: dict[str, dict] = {}
    for cloud in wanted:
        cloud_creds = getattr(creds, cloud)
        if cloud_creds is None:
            result[cloud] = {"status": "not_configured"}
            continue
        try:
            if cloud == "azure":
                accounts, deployments = await fetch_azure_deployments(
                    cloud_creds, transport=transport, sleep=sleep
                )
                summary: dict = {"accounts": accounts}
            else:
                summary, deployments = await fetch_aws_deployments(
                    cloud_creds, transport=transport, sleep=sleep
                )
        except CloudInventoryError as exc:
            logger.warning("cloud inventory failed for tenant %s: %s", tenant_id, exc)
            result[cloud] = {"status": "error", "error": str(exc)}
            continue
        counts = await store_inventory(db, tenant_id, deployments, now)
        await db.commit()
        logger.info("cloud inventory: tenant=%s cloud=%s %s", tenant_id, cloud, counts)
        result[cloud] = {
            "status": "ok",
            **summary,
            "deployments": len(deployments),
            **counts,
        }
    return {"tenant_id": str(tenant_id), "clouds": result}


router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


class CloudInventoryPullBody(BaseModel):
    tenant_id: UUID
    clouds: list[str] | None = None


@router.post(
    "/cloud-inventory/pull",
    summary="Inventory AI model deployments in cloud accounts (operator)",
    dependencies=[Depends(require_operator)],
)
async def pull_cloud_inventory_endpoint(
    body: CloudInventoryPullBody, request: Request
) -> dict:
    db = request.state.db
    if await db.get(Tenant, body.tenant_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "about:blank",
                "title": "Not Found",
                "status": 404,
                "detail": "Tenant not found",
            },
        )
    return await pull_cloud_inventory(db, body.tenant_id, clouds=body.clouds)


async def _main(tenant: UUID, clouds: list[str] | None) -> dict:
    from collector.app import SessionLocal, engine

    try:
        async with SessionLocal() as db:
            return await pull_cloud_inventory(db, tenant, clouds=clouds)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tenant", type=UUID, required=True)
    parser.add_argument("--cloud", action="append", choices=SUPPORTED_CLOUDS)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_main(args.tenant, args.cloud)), indent=2))


if __name__ == "__main__":
    main()
