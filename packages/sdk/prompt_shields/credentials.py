"""Stable credential fingerprints for keyless provider auth (#76).

By default the SDK reports ``api_key_fingerprint = sha256(api_key)[:16]``.
With keyless auth (OAuth client credentials, workload identity federation,
cloud role credentials) the credential the provider sees is a short-lived
token, so hashing it gives a new value at every rotation and calls from one
workload no longer group together.

These helpers hash the *identity* the credential belongs to instead. They
compute exactly what the gateway reports with
``PS_TELEMETRY_CREDENTIAL_FINGERPRINT=true`` (``gateway/src/utils/
credentialFingerprint.ts``): ``sha256("<kind>\\n<part>\\n...")[:16]`` for an
identity, and ``sha256(key)[:16]`` for a static key, so SDK and gateway events
for one identity carry one fingerprint. Identity fingerprints never collide
with key fingerprints because of the ``<kind>`` prefix.

Opt-in: pass the value as ``credential_fingerprint=`` to a client or to
``instrument()`` (or set ``PS_CREDENTIAL_FINGERPRINT``). Nothing here reads,
stores or sends a secret; the inputs are identifiers only (client id, role
ARN, service account id).

    from prompt_shields import ShieldsAnthropic
    from prompt_shields.credentials import anthropic_workload_fingerprint

    client = ShieldsAnthropic(
        api_key=None,               # the Anthropic SDK federates on its own
        ps_api_key="ps-...",
        credential_fingerprint=anthropic_workload_fingerprint(org_id, svac_id),
    )
"""

from __future__ import annotations

import hashlib
import os

__all__ = [
    "anthropic_workload_fingerprint",
    "anthropic_workload_fingerprint_from_env",
    "aws_key_fingerprint",
    "aws_role_fingerprint",
    "azure_entra_fingerprint",
    "azure_managed_fingerprint",
    "azure_workload_fingerprint",
    "gcp_service_account_fingerprint",
    "identity_fingerprint",
    "key_fingerprint",
    "oauth_client_fingerprint",
    "openai_workload_fingerprint",
]


def _sha16(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _require(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    # Not stripped: the gateway hashes the value as given.
    return value


def key_fingerprint(api_key: str) -> str:
    """``sha256(api_key)[:16]``: the default fingerprint of a static key."""
    return _sha16(api_key)


def identity_fingerprint(kind: str, *parts: str) -> str:
    """``sha256("<kind>\\n<parts...>")[:16]``, the gateway's identity hash."""
    return _sha16("\n".join([kind, *parts]))


def oauth_client_fingerprint(token_url: str, client_id: str) -> str:
    """OAuth 2.0 client credentials (gateway ``openai`` / AWS ``oauth``).

    The secret and scope are deliberately not part of the identity, so the
    value survives secret rotation too."""
    return identity_fingerprint(
        "oauth-client", _require("token_url", token_url), _require("client_id", client_id)
    )


def aws_role_fingerprint(role_arn: str) -> str:
    """An assumed AWS role (not the minted session keys)."""
    return identity_fingerprint("aws-role", _require("role_arn", role_arn))


def aws_key_fingerprint(access_key_id: str) -> str:
    """Static AWS keys, by access key id (never the secret)."""
    return identity_fingerprint("aws-key", _require("access_key_id", access_key_id))


def azure_entra_fingerprint(tenant_id: str, client_id: str) -> str:
    """An Entra ID app registration (client secret or certificate)."""
    return identity_fingerprint(
        "azure-entra", _require("tenant_id", tenant_id), _require("client_id", client_id)
    )


def azure_managed_fingerprint(client_id: str) -> str:
    """A user-assigned managed identity, by client id."""
    return identity_fingerprint("azure-managed", _require("client_id", client_id))


def azure_workload_fingerprint(client_id: str) -> str:
    """Entra Workload ID (federated token), by client id."""
    return identity_fingerprint("azure-workload", _require("client_id", client_id))


def gcp_service_account_fingerprint(client_email: str) -> str:
    """A Google service account, by its e-mail."""
    return identity_fingerprint("gcp-sa", _require("client_email", client_email))


def openai_workload_fingerprint(service_account_id: str) -> str:
    """OpenAI workload identity federation, by OpenAI service account id."""
    return identity_fingerprint(
        "openai-workload", _require("service_account_id", service_account_id)
    )


def anthropic_workload_fingerprint(organization_id: str, service_account_id: str) -> str:
    """Anthropic workload identity federation (``svac_...`` in an org)."""
    return identity_fingerprint(
        "anthropic-workload",
        _require("organization_id", organization_id),
        _require("service_account_id", service_account_id),
    )


def anthropic_workload_fingerprint_from_env() -> str | None:
    """From ``ANTHROPIC_ORGANIZATION_ID`` / ``ANTHROPIC_SERVICE_ACCOUNT_ID``,
    the variables the Anthropic SDK reads for zero-argument federation.
    ``None`` when either is unset."""
    org = os.environ.get("ANTHROPIC_ORGANIZATION_ID", "").strip()
    sa = os.environ.get("ANTHROPIC_SERVICE_ACCOUNT_ID", "").strip()
    return anthropic_workload_fingerprint(org, sa) if org and sa else None
