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
  - GCP: Vertex AI models deployed to endpoints (including Model Garden
    deployments) and tuned models, per configured project and region, via
    the regional Vertex AI API with a service-account JWT-bearer token.

Each asset's ``calling_service`` is ``azure:<ARM deployment resource id>``,
``aws:<Bedrock resource ARN>`` or ``gcp:<Vertex AI resource name>``, which
marks where it came from. Asset ids are UUIDv5 values derived from the
tenant, the cloud and that resource id, so re-runs update the same rows.

Run it with ``POST /api/v1/admin/cloud-inventory/pull`` (operator only:
``Authorization: Bearer $PS_ADMIN_TOKEN``, ``{"tenant_id": ...}``; 404 when
the token is unset) or ``python -m collector.cloud_inventory --tenant <uuid>``.

Credentials come from the operator's environment only, per tenant, in
``PS_CLOUD_INVENTORY`` (JSON, see ``CloudInventorySettings``); never from a
request body. Short-lived credentials (assumed AWS roles, GCP access tokens)
are renewed shortly before they expire, so long runs do not fail part-way.
A failing Azure subscription, AWS region or GCP project/region is reported
and does not stop the others. Secrets are held as ``SecretStr`` and are
never logged or returned. HTTP calls go only to the fixed Microsoft hosts below, and to
``bedrock.<region>.amazonaws.com`` / ``sts.<region>.amazonaws.com`` for
strictly validated region names, and to ``oauth2.googleapis.com`` /
``<region>-aiplatform.googleapis.com`` (likewise validated); ARM
``nextLink`` URLs, resource ids, ARNs and Vertex AI resource names are
checked before they are followed or stored.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from db.models import AIAsset, Tenant
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings
from sqlalchemy.ext.asyncio import AsyncSession

from collector import rls
from collector.dedup import compute_confidence
from collector.lifecycle import reactivate
from collector.provider_usage_admin import require_operator

logger = logging.getLogger("prompt_shields.cloud_inventory")

SOURCE = "platform_signal"
SUPPORTED_CLOUDS = ("azure", "aws", "gcp")

# Fixed hosts. Never derived from tenant input.
AZURE_LOGIN_HOST = "login.microsoftonline.com"
AZURE_ARM_HOST = "management.azure.com"
AZURE_ARM_SCOPE = "https://management.azure.com/.default"
ARM_SUBSCRIPTIONS_API = "2022-12-01"
ARM_COGNITIVE_API = "2024-10-01"
# Account kinds that host model deployments.
AZURE_AI_KINDS = {"openai", "aiservices"}
# Deployments in these states are not serving and are not inventoried.
INACTIVE_STATES = {"failed", "deleting", "deleted", "canceled", "disabled"}

# AWS: hosts are built only from a validated region name.
AWS_BEDROCK_HOST = "bedrock.{region}.amazonaws.com"
AWS_STS_HOST = "sts.{region}.amazonaws.com"
STS_API_VERSION = "2011-06-15"
# Assumed-role session length (AssumeRole DurationSeconds); configurable per
# tenant. The role's maximum session duration must allow it (IAM default 1h).
STS_SESSION_SECONDS = 3600
STS_MIN_SESSION_SECONDS = 900
STS_MAX_SESSION_SECONDS = 43200
# Short-lived credentials (assumed-role keys, GCP access tokens) are renewed
# when less than this is left, so a long run never signs with expired ones.
CREDENTIAL_REFRESH_MARGIN_S = 300
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
# A custom model is an asset only once its training job has finished.
AWS_CUSTOM_MODEL_UNUSABLE = AWS_INACTIVE_STATES | {"creating"}

# GCP: the token endpoint is fixed (a key file's ``token_uri`` is ignored);
# Vertex AI hosts are built only from a validated region name.
GCP_TOKEN_URL = "https://oauth2.googleapis.com/token"
GCP_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
GCP_JWT_LIFETIME_S = 600
# Assumed when a token response carries no usable ``expires_in``.
GCP_TOKEN_DEFAULT_LIFETIME_S = 3600
VERTEX_HOST = "{region}-aiplatform.googleapis.com"
VERTEX_PAGE_SIZE = 100
# Vertex AI regions, e.g. "us-central1", "europe-west4", "me-central2",
# "northamerica-northeast1". No "global" and no multi-regions.
_GCP_REGION = re.compile(
    r"^(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-"
    r"(?:north|south|east|west|central|northeast|northwest|southeast|southwest)"
    r"[1-9][0-9]?$"
)
_GCP_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_GCP_SA_EMAIL = re.compile(
    r"^[a-z0-9-]{1,100}@[a-z0-9.-]{1,100}\.gserviceaccount\.com$"
)
_GCP_PEM = re.compile(r"-----BEGIN (?:RSA )?PRIVATE KEY-----")
# A project id or number in a Vertex AI resource name.
_GCP_SEGMENT = r"[a-z0-9][a-z0-9-]{0,62}"
_GCP_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_VERTEX_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/-]{0,199}$")

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


def aws_partition(region: str) -> str:
    """``aws-us-gov`` for a GovCloud region, else ``aws`` (only the
    amazonaws.com partitions pass ``_AWS_REGION``)."""
    return "aws-us-gov" if region.startswith("us-gov-") else "aws"


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
    # Assumed-role session length; renewed before expiry on long runs.
    session_duration_seconds: int = STS_SESSION_SECONDS
    # Bedrock is regional: every region to inventory must be listed. All in
    # one partition (commercial or GovCloud): keys and roles never span both.
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

    @field_validator("session_duration_seconds")
    @classmethod
    def _duration(cls, v: int) -> int:
        if not STS_MIN_SESSION_SECONDS <= v <= STS_MAX_SESSION_SECONDS:
            raise ValueError(
                f"session_duration_seconds must be {STS_MIN_SESSION_SECONDS}"
                f"-{STS_MAX_SESSION_SECONDS}"
            )
        return v

    @model_validator(mode="after")
    def _external_id_needs_role(self):
        if self.external_id is not None and self.role_arn is None:
            raise ValueError("external_id requires role_arn")
        return self

    @model_validator(mode="after")
    def _one_partition(self):
        # Access keys, roles and STS are per partition: a commercial key
        # cannot sign in GovCloud or assume a role there, and vice versa.
        partitions = {aws_partition(r) for r in self.regions}
        if len(partitions) > 1:
            raise ValueError(
                "regions mix AWS partitions (commercial and GovCloud); "
                "configure one partition per credential"
            )
        if self.role_arn is not None:
            role_partition = self.role_arn.split(":", 2)[1]
            if role_partition not in partitions:
                raise ValueError("role_arn is in another partition than regions")
        return self


class GcpServiceAccountKey(BaseModel):
    """The fields of a service-account JSON key file that are used. Others
    (including ``token_uri``) are ignored: the token host is fixed."""

    model_config = {"extra": "ignore", "hide_input_in_errors": True}

    type: str = "service_account"
    project_id: str | None = None
    client_email: str
    private_key_id: SecretStr | None = None
    private_key: SecretStr

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v != "service_account":
            raise ValueError("only service account keys are supported")
        return v

    @field_validator("client_email")
    @classmethod
    def _email(cls, v: str) -> str:
        if not _GCP_SA_EMAIL.match(v):
            raise ValueError("client_email must be a service account email")
        return v

    @field_validator("private_key")
    @classmethod
    def _key(cls, v: SecretStr) -> SecretStr:
        if not _GCP_PEM.search(v.get_secret_value()):
            raise ValueError("private_key must be a PEM private key")
        return v


class GcpInventoryCredentials(BaseModel):
    """A service account with ``roles/aiplatform.viewer`` (see the guide)."""

    model_config = {"hide_input_in_errors": True}

    # The JSON key file, as an object or as a JSON string.
    service_account: GcpServiceAccountKey
    # Empty: the service account's own project.
    project_ids: list[str] = []
    # Vertex AI is regional: every region to inventory must be listed.
    regions: list[str]

    @field_validator("service_account", mode="before")
    @classmethod
    def _parse_key(cls, v):
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = None
        if not isinstance(v, dict):
            # Not echoing the value: it may be (part of) the key.
            raise ValueError(  # noqa: TRY004 (pydantic needs ValueError)
                "service_account must be a service account JSON key"
            )
        return v

    @field_validator("project_ids")
    @classmethod
    def _projects(cls, v: list[str]) -> list[str]:
        if len(v) > 50 or not all(_GCP_PROJECT_ID.match(p) for p in v):
            raise ValueError("project_ids must be up to 50 GCP project ids")
        return list(dict.fromkeys(v))

    @field_validator("regions")
    @classmethod
    def _regions(cls, v: list[str]) -> list[str]:
        if not v or len(v) > 40 or not all(_GCP_REGION.match(r) for r in v):
            raise ValueError("regions must be 1-40 Vertex AI region names")
        return list(dict.fromkeys(v))

    @model_validator(mode="after")
    def _default_project(self):
        if not self.project_ids:
            project = self.service_account.project_id
            if not project or not _GCP_PROJECT_ID.match(project):
                raise ValueError("project_ids is required")
            self.project_ids = [project]
        return self


class TenantCloudCredentials(BaseModel):
    azure: AzureInventoryCredentials | None = None
    aws: AwsInventoryCredentials | None = None
    gcp: GcpInventoryCredentials | None = None


class CloudInventorySettings(BaseSettings):
    """``PS_CLOUD_INVENTORY='{"<tenant uuid>": {"azure": {"directory_id": ...,
    "client_id": ..., "client_secret": ..., "subscription_ids": [...]},
    "aws": {"access_key_id": ..., "secret_access_key": ..., "role_arn": ...,
    "external_id": ..., "regions": [...]}, "gcp": {"service_account": {<key
    file JSON>}, "project_ids": [...], "regions": [...]}}}'``"""

    cloud_inventory: dict[str, TenantCloudCredentials] = {}

    # A malformed value must not echo the client secret in the error.
    model_config = {
        "env_prefix": "PS_",
        "extra": "ignore",
        "hide_input_in_errors": True,
    }

    @field_validator("cloud_inventory")
    @classmethod
    def _tenant_keys(
        cls, v: dict[str, TenantCloudCredentials]
    ) -> dict[str, TenantCloudCredentials]:
        # Tenant ids are matched in canonical form, so an upper-case (or
        # brace-wrapped) UUID key is not silently ignored.
        out: dict[str, TenantCloudCredentials] = {}
        for key, creds in v.items():
            try:
                tenant = str(UUID(key.strip()))
            except ValueError:
                raise ValueError("cloud_inventory keys must be tenant UUIDs")
            if tenant in out:
                raise ValueError("cloud_inventory has a tenant more than once")
            out[tenant] = creds
        return out


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
        items = body.get("value") if isinstance(body, dict) else None
        if not isinstance(body, dict) or not isinstance(items, list | None):
            raise CloudInventoryError(f"azure: {what} returned an unexpected body")
        for item in items or []:
            if not isinstance(item, dict):
                raise CloudInventoryError(f"azure: {what} returned a malformed item")
            yield item
        next_link = body.get("nextLink")
        if next_link is not None and not isinstance(next_link, str):
            raise CloudInventoryError(f"azure: {what} returned a malformed nextLink")
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
    token = body.get("access_token") if isinstance(body, dict) else None
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
) -> tuple[dict, list[CloudDeployment]]:
    """Return (summary, model deployments) for one service principal.

    A failing subscription (e.g. 403 where the principal lacks ``Reader``)
    is reported in ``summary["subscription_errors"]`` and does not stop the
    others. A failing token request or subscription listing, or every
    subscription failing, raises ``CloudInventoryError``.
    """
    deployments: list[CloudDeployment] = []
    errors: dict[str, str] = {}
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
            try:
                accounts, found = await _azure_subscription(client, sid, headers, sleep)
            except CloudInventoryError as exc:
                errors[sid] = str(exc)
                continue
            accounts_seen += accounts
            deployments += found
    if subscriptions and len(errors) == len(subscriptions):
        raise CloudInventoryError("; ".join(dict.fromkeys(errors.values())))
    summary: dict = {"accounts": accounts_seen}
    if errors:
        summary["subscription_errors"] = errors
    return summary, deployments


def _str_or_none(value) -> str | None:
    return value if isinstance(value, str) and value else None


async def _azure_subscription(
    client: httpx.AsyncClient,
    sid: str,
    headers: dict[str, str],
    sleep: Sleep,
) -> tuple[int, list[CloudDeployment]]:
    """(AI accounts seen, model deployments) in one subscription."""
    deployments: list[CloudDeployment] = []
    accounts_seen = 0
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
        region = _str_or_none(account.get("location"))
        account_tags = _tags(account.get("tags"))
        async for dep in _arm_list(
            client,
            f"{account_id}/deployments",
            ARM_COGNITIVE_API,
            headers,
            "deployment list",
            sleep,
        ):
            props = dep.get("properties")
            props = props if isinstance(props, dict) else {}
            # "Disabled" (e.g. the account's quota or subscription was
            # disabled) is not serving, like Failed or Deleting.
            state = str(props.get("provisioningState") or "").lower()
            name = dep.get("name")
            if state in INACTIVE_STATES or not isinstance(name, str) or not name:
                continue
            model = props.get("model")
            model = model if isinstance(model, dict) else {}
            deployments.append(
                CloudDeployment(
                    cloud="azure",
                    resource_id=f"{account_id}/deployments/{name}",
                    region=region.lower() if region else None,
                    model=_str_or_none(model.get("name")),
                    model_format=_str_or_none(model.get("format")),
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
    keys: AwsKeys | _AwsAuth,
    what: str,
    sleep: Sleep,
    clock: Clock,
    params: dict[str, str] | None = None,
    body: bytes = b"",
    content_type: str | None = None,
    as_text: bool = False,
):
    if isinstance(keys, _AwsAuth):
        # Checked per call: renews assumed-role keys close to expiry.
        keys = await keys.keys()
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
) -> tuple[AwsKeys, datetime]:
    """Short-lived credentials for ``creds.role_arn`` and when they expire,
    from the regional STS endpoint of the first configured region (all
    regions are in one partition, so that endpoint is in the right one)."""
    region = creds.regions[0]
    issued = clock()
    form = {
        "Action": "AssumeRole",
        "Version": STS_API_VERSION,
        "RoleArn": creds.role_arn,
        "RoleSessionName": creds.role_session_name,
        "DurationSeconds": str(creds.session_duration_seconds),
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
    # Never trust an expiry later than what was asked for.
    expires = issued + timedelta(seconds=creds.session_duration_seconds)
    try:
        stated = datetime.fromisoformat(_xml_text(root, "Expiration") or "")
    except ValueError:
        stated = None
    if stated is not None and stated.tzinfo is not None:
        expires = min(expires, stated)
    return AwsKeys(key_id, secret, token), expires


class _AwsAuth:
    """The keys Bedrock calls are signed with: the configured keys, or
    assumed-role session keys, renewed shortly before they expire so a run
    longer than one session does not fail part-way."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        creds: AwsInventoryCredentials,
        sleep: Sleep,
        clock: Clock,
    ):
        self._client, self._creds, self._sleep, self._clock = client, creds, sleep, clock
        self._base = AwsKeys(
            creds.access_key_id,
            creds.secret_access_key.get_secret_value(),
            creds.session_token.get_secret_value() if creds.session_token else None,
        )
        self._keys: AwsKeys | None = None if creds.role_arn else self._base
        self._expires: datetime | None = None

    async def keys(self) -> AwsKeys:
        if self._creds.role_arn is None:
            return self._base
        margin = timedelta(seconds=CREDENTIAL_REFRESH_MARGIN_S)
        if self._keys is None or self._clock() >= self._expires - margin:
            self._keys, self._expires = await _aws_assume_role(
                self._client, self._creds, self._base, self._sleep, self._clock
            )
        return self._keys


async def _bedrock_list(
    client: httpx.AsyncClient,
    region: str,
    path: str,
    items_key: str,
    keys: AwsKeys | _AwsAuth,
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
    keys: AwsKeys | _AwsAuth,
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
    keys: AwsKeys | _AwsAuth,
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
        # Still training ("Creating") or failed: not (yet) an asset.
        status = str(m.get("modelStatus") or "").lower()
        if arn is None or status in AWS_CUSTOM_MODEL_UNUSABLE:
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
    keys: AwsKeys | _AwsAuth,
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
    failing, raises ``CloudInventoryError``. Assumed-role keys are renewed
    before they expire.
    """
    deployments: list[CloudDeployment] = []
    errors: dict[str, str] = {}
    logging_enabled: dict[str, bool] = {}
    async with httpx.AsyncClient(
        transport=transport, timeout=30.0, follow_redirects=False
    ) as client:
        keys = _AwsAuth(client, creds, sleep, clock)
        # Fail the whole cloud (not each region) when the role is refused.
        await keys.keys()
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


# --- Google Cloud Vertex AI -------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def gcp_jwt_assertion(key: GcpServiceAccountKey, now: int) -> str:
    """An RS256 JWT for the OAuth 2.0 JWT-bearer grant (RFC 7523)."""
    header = {"alg": "RS256", "typ": "JWT"}
    if key.private_key_id is not None:
        header["kid"] = key.private_key_id.get_secret_value()
    claims = {
        "iss": key.client_email,
        "scope": GCP_SCOPE,
        "aud": GCP_TOKEN_URL,
        "iat": now,
        "exp": now + GCP_JWT_LIFETIME_S,
    }
    signing_input = ".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in (header, claims)
    )
    try:
        private_key = serialization.load_pem_private_key(
            key.private_key.get_secret_value().encode(), password=None
        )
    except (ValueError, TypeError):
        private_key = None
    if not isinstance(private_key, rsa.RSAPrivateKey):
        # No detail from the parser: it must not quote the key.
        raise CloudInventoryError("gcp: service account private_key is unusable")
    signature = private_key.sign(
        signing_input.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{signing_input}.{_b64url(signature)}"


async def _gcp_token(
    client: httpx.AsyncClient,
    key: GcpServiceAccountKey,
    sleep: Sleep,
    clock: Callable[[], float],
) -> tuple[str, float]:
    """An access token and its lifetime in seconds."""
    body = await _request(
        client,
        "POST",
        GCP_TOKEN_URL,
        what="token request",
        sleep=sleep,
        cloud="gcp",
        data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": gcp_jwt_assertion(key, int(clock())),
        },
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise CloudInventoryError("gcp: token response had no access_token")
    lifetime = body.get("expires_in")
    if (
        isinstance(lifetime, bool)
        or not isinstance(lifetime, int | float)
        or lifetime <= 0
    ):
        lifetime = GCP_TOKEN_DEFAULT_LIFETIME_S
    return token, min(float(lifetime), GCP_TOKEN_DEFAULT_LIFETIME_S)


class _GcpAuth:
    """Bearer headers for Vertex AI calls. The access token (about an hour)
    is exchanged again shortly before it expires, so a run over many
    projects and regions does not fail part-way."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        key: GcpServiceAccountKey,
        sleep: Sleep,
        clock: Callable[[], float],
    ):
        self._client, self._key, self._sleep, self._clock = client, key, sleep, clock
        self._token: str | None = None
        self._expires = 0.0

    async def headers(self) -> dict[str, str]:
        if (
            self._token is None
            or self._clock() >= self._expires - CREDENTIAL_REFRESH_MARGIN_S
        ):
            issued = self._clock()
            self._token, lifetime = await _gcp_token(
                self._client, self._key, self._sleep, self._clock
            )
            self._expires = issued + lifetime
        return {"Authorization": f"Bearer {self._token}"}


async def _vertex_list(
    client: httpx.AsyncClient,
    region: str,
    parent: str,
    collection: str,
    auth: _GcpAuth,
    what: str,
    sleep: Sleep,
):
    # The host comes from the validated region only; the page token is a
    # query parameter, so it cannot change where the request goes.
    url = f"https://{VERTEX_HOST.format(region=region)}/v1/{parent}/{collection}"
    params = {"pageSize": str(VERTEX_PAGE_SIZE)}
    for _ in range(MAX_PAGES):
        body = await _request(
            client,
            "GET",
            url,
            what=what,
            sleep=sleep,
            cloud="gcp",
            params=params,
            headers=await auth.headers(),
        )
        items = body.get(collection) if isinstance(body, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                yield item
        token = body.get("nextPageToken") if isinstance(body, dict) else None
        if not token:
            return
        if not isinstance(token, str) or len(token) > 4096:
            raise CloudInventoryError(f"gcp: {what}: unexpected nextPageToken")
        params = {"pageSize": str(VERTEX_PAGE_SIZE), "pageToken": token}
    raise CloudInventoryError(f"gcp: {what}: more than {MAX_PAGES} pages")


def _vertex_name(value, region: str, collection: str) -> str | None:
    """A Vertex AI resource name in ``region``, without any ``@version``,
    or None. The project segment may be an id or a number."""
    if not isinstance(value, str):
        return None
    name = value.split("@", 1)[0]
    pattern = (
        rf"^projects/{_GCP_SEGMENT}/locations/{re.escape(region)}/"
        rf"{collection}/[A-Za-z0-9_-]{{1,128}}$"
    )
    return name if re.match(pattern, name) else None


_VERTEX_MODEL_NAME = re.compile(
    rf"^projects/{_GCP_SEGMENT}/locations/(?P<region>[a-z0-9-]{{1,40}})/"
    r"models/[A-Za-z0-9_-]{1,128}$"
)


def _vertex_model_ref(value) -> tuple[str, str] | None:
    """(Model Registry name without ``@version``, its region) for a model in
    any valid Vertex AI region, or None."""
    if not isinstance(value, str):
        return None
    name = value.split("@", 1)[0]
    m = _VERTEX_MODEL_NAME.match(name)
    if not m or not _GCP_REGION.match(m["region"]):
        return None
    return name, m["region"]


def _vertex_model_id(value) -> str | None:
    """``gemini-2.0-flash-001`` from a bare id or from a publisher model name
    such as ``publishers/google/models/gemma2``."""
    if not isinstance(value, str):
        return None
    if "models/" in value:
        value = value.rsplit("models/", 1)[1]
    value = value.strip()
    return value if _VERTEX_MODEL_ID.match(value) else None


def _display_name(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:200]


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


@dataclass
class _VertexCatalog:
    """Tuning jobs and Model Registry entries read in this run, across
    projects and regions (their resource names include both), so that a
    deployed model is named even when it lives in another region than its
    endpoint."""

    # Tuned model name -> (base model id, the tuned model it continued
    # tuning from, tuning job labels, region).
    tuned: dict[str, tuple[str | None, str | None, dict[str, str], str]] = field(
        default_factory=dict
    )
    # Model Registry name -> Model resource.
    models: dict[str, dict] = field(default_factory=dict)
    # Model names already looked up one by one.
    fetched: set[str] = field(default_factory=set)

    def base_model(self, name: str) -> str | None:
        """The base model of a tuned model; for continuous tuning (a job
        with ``preTunedModel`` and no ``baseModel``), that of the tuned
        model it started from."""
        seen: set[str] = set()
        while name in self.tuned and name not in seen:
            seen.add(name)
            base, parent, _, _ = self.tuned[name]
            if base or parent is None:
                return base
            name = parent
        return None

    def describe(self, name: str | None) -> tuple[str | None, str, dict[str, str]]:
        """(model, kind, labels) for what a deployed model serves: the base
        model of a tuned model, the publisher model of a Model Garden
        deployment, or the registry display name of a custom model."""
        if name is None:
            return None, "custom", {}
        m = self.models.get(name, {})
        labels = _tags(m.get("labels"))
        if name in self.tuned:
            return self.base_model(name), "tuned", labels
        garden = _vertex_model_id(
            _dict(_dict(m.get("baseModelSource")).get("modelGardenSource")).get(
                "publicModelName"
            )
        )
        if garden:
            return garden, "model_garden", labels
        return _display_name(m.get("displayName")), "custom", labels


async def _vertex_catalog(
    client: httpx.AsyncClient,
    project: str,
    region: str,
    auth: _GcpAuth,
    sleep: Sleep,
    catalog: _VertexCatalog,
) -> None:
    """Add one project and region's succeeded tuning jobs and Model
    Registry to ``catalog`` (only once both were read in full)."""
    parent = f"projects/{project}/locations/{region}"

    # Tuned models (supervised tuning of Gemini or open models): trained on
    # the tenant's data, so an asset even before (or without) serving.
    tuned: dict[str, tuple[str | None, str | None, dict[str, str], str]] = {}
    async for job in _vertex_list(
        client, region, parent, "tuningJobs", auth, "tuning job list", sleep
    ):
        if job.get("state") != "JOB_STATE_SUCCEEDED":
            continue
        model_name = _vertex_name(
            _dict(job.get("tunedModel")).get("model"), region, "models"
        )
        if model_name is None:
            continue
        # Continuous tuning starts from an earlier tuned model and may
        # leave baseModel empty.
        pre = _dict(job.get("preTunedModel"))
        pre_ref = _vertex_model_ref(pre.get("tunedModelName"))
        tuned[model_name] = (
            _vertex_model_id(job.get("baseModel"))
            or _vertex_model_id(pre.get("baseModel")),
            pre_ref[0] if pre_ref else None,
            _tags(job.get("labels")),
            region,
        )

    models: dict[str, dict] = {}
    async for m in _vertex_list(
        client, region, parent, "models", auth, "model list", sleep
    ):
        name = _vertex_name(m.get("name"), region, "models")
        if name is not None:
            models[name] = m

    catalog.tuned.update(tuned)
    catalog.models.update(models)


async def _vertex_fetch_model(
    client: httpx.AsyncClient,
    name: str,
    region: str,
    auth: _GcpAuth,
    sleep: Sleep,
    catalog: _VertexCatalog,
) -> None:
    """Read one Model Registry entry in a region that was not inventoried.
    A failure leaves the model unnamed; it does not fail the endpoint."""
    if name in catalog.fetched:
        return
    catalog.fetched.add(name)
    # ``name`` and ``region`` are validated (``_vertex_model_ref``).
    try:
        m = await _request(
            client,
            "GET",
            f"https://{VERTEX_HOST.format(region=region)}/v1/{name}",
            what="model get",
            sleep=sleep,
            cloud="gcp",
            headers=await auth.headers(),
        )
    except CloudInventoryError as exc:
        logger.warning("gcp: could not read a deployed model (%s): %s", region, exc)
        return
    if isinstance(m, dict) and _vertex_name(m.get("name"), region, "models") == name:
        catalog.models[name] = m


async def _vertex_endpoints(
    client: httpx.AsyncClient,
    project: str,
    region: str,
    auth: _GcpAuth,
    sleep: Sleep,
    catalog: _VertexCatalog,
) -> list[CloudDeployment]:
    """Models deployed to endpoints in one project and region. Each is
    serving capacity (dedicated or shared), the Vertex AI counterpart of an
    Azure deployment."""
    parent = f"projects/{project}/locations/{region}"
    found: list[CloudDeployment] = []
    async for ep in _vertex_list(
        client, region, parent, "endpoints", auth, "endpoint list", sleep
    ):
        endpoint = _vertex_name(ep.get("name"), region, "endpoints")
        if endpoint is None:
            logger.warning("gcp: skipping endpoint with unexpected name")
            continue
        endpoint_labels = _tags(ep.get("labels"))
        for dm in ep.get("deployedModels") or []:
            if not isinstance(dm, dict):
                continue
            dm_id = dm.get("id")
            if not isinstance(dm_id, str) or not _GCP_ID.match(dm_id):
                continue
            ref = _vertex_model_ref(dm.get("model"))
            model_name = ref[0] if ref else None
            if (
                ref is not None
                and ref[1] != region
                and model_name not in catalog.models
                and model_name not in catalog.tuned
            ):
                await _vertex_fetch_model(client, *ref, auth, sleep, catalog)
            model, kind, model_labels = catalog.describe(model_name)
            found.append(
                CloudDeployment(
                    cloud="gcp",
                    resource_id=f"{endpoint}/deployedModels/{dm_id}",
                    # Where it is served: the endpoint's region.
                    region=region,
                    model=model or _display_name(dm.get("displayName")),
                    model_format=kind,
                    # The endpoint's labels override the model's.
                    tags={**model_labels, **endpoint_labels},
                )
            )
    return found


async def fetch_gcp_deployments(
    creds: GcpInventoryCredentials,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.time,
) -> tuple[dict, list[CloudDeployment]]:
    """Return (summary, Vertex AI resources) for one service account.

    A failing project/region is reported in ``summary["region_errors"]``
    and does not stop the others. A failing token exchange, or every
    project/region failing, raises ``CloudInventoryError``. The access
    token is renewed before it expires.
    """
    deployments: list[CloudDeployment] = []
    errors: dict[str, str] = {}
    catalog = _VertexCatalog()
    async with httpx.AsyncClient(
        transport=transport, timeout=30.0, follow_redirects=False
    ) as client:
        auth = _GcpAuth(client, creds.service_account, sleep, clock)
        # Fail the whole cloud (not each region) when the token is refused.
        await auth.headers()
        # Every region's registry first, so endpoints can name a model that
        # lives in another region than the endpoint.
        read: list[tuple[str, str]] = []
        for project in creds.project_ids:
            for region in creds.regions:
                try:
                    await _vertex_catalog(client, project, region, auth, sleep, catalog)
                    read.append((project, region))
                except CloudInventoryError as exc:
                    errors[f"{project}/{region}"] = str(exc)

        for name, (_, _, job_labels, region) in catalog.tuned.items():
            deployments.append(
                CloudDeployment(
                    cloud="gcp",
                    resource_id=name,
                    region=region,
                    model=catalog.base_model(name),
                    model_format="tuned_model",
                    # The registry model's labels override the tuning job's.
                    tags={
                        **job_labels,
                        **_tags(catalog.models.get(name, {}).get("labels")),
                    },
                )
            )

        for project, region in read:
            try:
                deployments += await _vertex_endpoints(
                    client, project, region, auth, sleep, catalog
                )
            except CloudInventoryError as exc:
                errors[f"{project}/{region}"] = str(exc)
    if len(errors) == len(creds.project_ids) * len(creds.regions):
        raise CloudInventoryError("; ".join(dict.fromkeys(errors.values())))
    summary: dict = {
        "projects": len(creds.project_ids),
        "regions": len(creds.regions),
    }
    if errors:
        summary["region_errors"] = errors
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
    return {"azure": "microsoft", "aws": "amazon", "gcp": "google"}[d.cloud]


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


def unknown_clouds(clouds: list[str] | None) -> list[str]:
    return [c for c in dict.fromkeys(clouds or []) if c not in SUPPORTED_CLOUDS]


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
    An unknown name in ``clouds`` raises ``ValueError``.
    """
    unknown = unknown_clouds(clouds)
    if unknown:
        raise ValueError(f"unknown clouds: {', '.join(unknown)}")
    now = now or datetime.now(UTC)
    creds = credentials_for_tenant(tenant_id, settings)
    wanted = list(dict.fromkeys(clouds or SUPPORTED_CLOUDS))
    result: dict[str, dict] = {}
    for cloud in wanted:
        cloud_creds = getattr(creds, cloud)
        if cloud_creds is None:
            result[cloud] = {"status": "not_configured"}
            continue
        try:
            if cloud == "azure":
                summary, deployments = await fetch_azure_deployments(
                    cloud_creds, transport=transport, sleep=sleep
                )
            elif cloud == "aws":
                summary, deployments = await fetch_aws_deployments(
                    cloud_creds, transport=transport, sleep=sleep
                )
            else:
                summary, deployments = await fetch_gcp_deployments(
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
    unknown = unknown_clouds(body.clouds)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={
                "type": "about:blank",
                "title": "Bad Request",
                "status": 400,
                "detail": f"Unknown clouds: {', '.join(unknown)}. "
                f"Supported: {', '.join(SUPPORTED_CLOUDS)}",
            },
        )
    db = request.state.db
    # One tenant per pull: scope the session to it instead of bypassing RLS.
    await rls.set_tenant(db, body.tenant_id)
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
            await rls.set_tenant(db, tenant)
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
