"""Key-encryption key (KEK) providers for envelope encryption (#342).

The inference credential vault (collector.credential_vault) encrypts each
stored secret with its own random 256-bit data key (DEK) and stores the DEK
only *wrapped* by a KEK. A ``KeyProvider`` wraps and unwraps DEKs; it never
sees the secret itself. Each wrapped DEK carries a ``kek_ref`` naming the
provider and key (and, for Azure, the key version and algorithm) that
wrapped it, so data keys wrapped under an older KEK keep unwrapping while
``credential_vault.rewrap`` moves them to the current one, without
re-encrypting the secrets.

Providers (``PS_CREDENTIAL_KEK_PROVIDER``):

``local`` (default) — **development, tests and non-Azure deployments only;
not for production.** Wraps DEKs with AES-256-GCM under a key derived
(HKDF-SHA256) from ``PS_CREDENTIAL_KEK_KEYS``, a ``"kid:key,kid:key"``
keyring of its own: the first entry wraps, every entry unwraps. It is a
separate root from ``PS_WEBHOOK_SECRET_KEYS`` (webhook signing secrets and
provider admin keys), so leaking one keyring does not open the other. The
KEK lives in the collector's environment, next to the database credentials,
so it protects against a database-only leak and nothing more.
``kek_ref`` = ``local:<kid>``.

``azure-keyvault`` — Azure Key Vault or Managed HSM ``wrapKey`` /
``unwrapKey`` (REST API 7.4, over httpx). Only the 32-byte DEK is sent to
Key Vault, never the secret. The key is ``PS_AZURE_KEYVAULT_KEY_ID``
(``https://<vault>.vault.azure.net/keys/<name>[/<version>]``; without a
version, Key Vault wraps with the current version and the returned
versioned id is stored). Algorithm ``PS_AZURE_KEYVAULT_WRAP_ALGORITHM``
(RSA-OAEP-256 by default; A256KW for Managed HSM AES keys).
``kek_ref`` = ``azure-keyvault:<alg>:<versioned key id>``.

  - Hosts: the key URL must be ``https://<name>.<suffix>/keys/...`` with a
    suffix in ``AZURE_HOST_SUFFIXES`` (public, China and US Government
    clouds, Key Vault and Managed HSM). No port, query, credentials or
    other host, so a misconfiguration (or a tampered ``kek_ref`` row)
    cannot send a token or a data key anywhere else. The same check runs
    on every stored ``kek_ref`` before an unwrap and on the key id Key
    Vault returns.
  - Authentication: workload identity (``AZURE_FEDERATED_TOKEN_FILE``,
    AKS) or managed identity (``AZURE_CLIENT_ID`` picks a user-assigned
    one), through the optional ``azure-identity`` package
    (``pip install "prompt-shields-collector[azure-keyvault]"``). Client
    secrets in the environment are deliberately not supported.
  - Permissions: ``wrapKey``, ``unwrapKey`` and ``get`` on the key (the
    built-in "Key Vault Crypto User" role, or a Managed HSM local role with
    those data actions). ``get`` is used only to find the current version
    for ``credential_vault rewrap``.

Adding AWS KMS, GCP KMS or Vault Transit is a new class with the same four
methods and a ``kek_ref`` prefix, registered in ``_FACTORIES``.

Errors never contain key material: ``KeyProviderError`` messages carry the
provider, an HTTP status and the Key Vault error code at most.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag

from collector import secret_keyring
from collector.config import settings

logger = logging.getLogger(__name__)

DEK_BYTES = 32

LOCAL = "local"
AZURE_KEYVAULT = "azure-keyvault"
PROVIDERS = (LOCAL, AZURE_KEYVAULT)

LOCAL_KEYRING_ENV = "PS_CREDENTIAL_KEK_KEYS"
# HKDF purpose label: a local KEK never doubles as a webhook keyring key.
_LOCAL_INFO = b"prompt-shields credential kek v1"


class KeyProviderNotConfigured(RuntimeError):
    """The KEK provider is unknown, unconfigured or misconfigured."""


class KeyProviderError(RuntimeError):
    """A wrap or unwrap failed (unreachable key service, refused, wrong key
    or tampered data). The message never contains key material."""


@dataclass(frozen=True)
class WrappedKey:
    kek_ref: str
    wrapped: bytes


class KeyProvider(Protocol):
    name: str

    async def wrap(self, dek: bytes, context: bytes) -> WrappedKey:
        """Wrap ``dek`` under the current KEK. ``context`` is authenticated
        where the algorithm supports it (local); RSA-OAEP and A256KW do
        not, and the vault binds the payload to the same context anyway."""
        ...

    async def unwrap(self, kek_ref: str, wrapped: bytes, context: bytes) -> bytes:
        ...

    async def current_ref(self) -> str:
        """The ``kek_ref`` a wrap would carry now."""
        ...


def ref_provider(kek_ref: str) -> str:
    return kek_ref.split(":", 1)[0]


# ──────────────────────────────────────────────────────────────────────
# Local keyring (development only)
# ──────────────────────────────────────────────────────────────────────

_local_warned = False


class LocalKeyProvider:
    """AES-256-GCM key wrap under PS_CREDENTIAL_KEK_KEYS. Not for production."""

    name = LOCAL

    def _ring(self) -> list[tuple[str, str]]:
        return secret_keyring.parse_keyring(settings.credential_kek_keys, LOCAL_KEYRING_ENV)

    def _active(self) -> tuple[str, str]:
        try:
            ring = self._ring()
        except secret_keyring.KeyringNotConfigured as exc:
            raise KeyProviderNotConfigured(str(exc)) from None
        if not ring:
            raise KeyProviderNotConfigured(f"{LOCAL_KEYRING_ENV} is not set")
        return ring[0]

    async def current_ref(self) -> str:
        return f"{LOCAL}:{self._active()[0]}"

    async def wrap(self, dek: bytes, context: bytes) -> WrappedKey:
        global _local_warned
        kid, key = self._active()
        if not _local_warned:
            _local_warned = True
            logger.warning(
                "credential vault: PS_CREDENTIAL_KEK_PROVIDER=local wraps data keys "
                "with a key from the environment; use azure-keyvault in production"
            )
        nonce = secrets.token_bytes(12)
        ct = secret_keyring.derive(key, _LOCAL_INFO).encrypt(
            nonce, dek, context + b"." + kid.encode()
        )
        return WrappedKey(f"{LOCAL}:{kid}", nonce + ct)

    async def unwrap(self, kek_ref: str, wrapped: bytes, context: bytes) -> bytes:
        prefix, _, kid = kek_ref.partition(":")
        if prefix != LOCAL or not secret_keyring.KEY_ID_RE.fullmatch(kid):
            raise KeyProviderError("not a local kek_ref")
        try:
            key = dict(self._ring()).get(kid)
        except secret_keyring.KeyringNotConfigured as exc:
            raise KeyProviderNotConfigured(str(exc)) from None
        if key is None:
            raise KeyProviderNotConfigured(
                f"data key is wrapped by {LOCAL_KEYRING_ENV} key id {kid!r}, "
                "which this process does not have"
            )
        try:
            return secret_keyring.derive(key, _LOCAL_INFO).decrypt(
                wrapped[:12], wrapped[12:], context + b"." + kid.encode()
            )
        except (InvalidTag, ValueError):
            raise KeyProviderError("data key could not be unwrapped") from None


# ──────────────────────────────────────────────────────────────────────
# Azure Key Vault / Managed HSM
# ──────────────────────────────────────────────────────────────────────

API_VERSION = "7.4"
# Key Vault and Managed HSM DNS suffixes: public, China (21Vianet) and US
# Government clouds. Nothing else is ever contacted.
AZURE_HOST_SUFFIXES: tuple[str, ...] = (
    "vault.azure.net",
    "vault.azure.cn",
    "vault.usgovcloudapi.net",
    "managedhsm.azure.net",
    "managedhsm.azure.cn",
    "managedhsm.usgovcloudapi.net",
)
WRAP_ALGORITHMS: tuple[str, ...] = ("RSA-OAEP-256", "RSA-OAEP", "A256KW")
# Vault / HSM names: 3-24 characters, letters, digits and single hyphens,
# starting with a letter and not ending with a hyphen.
_VAULT_NAME = r"[a-z](?:[a-z0-9]|-(?!-)){1,22}[a-z0-9]"
_HOST_RE = re.compile(
    rf"({_VAULT_NAME})\.({'|'.join(re.escape(s) for s in AZURE_HOST_SUFFIXES)})"
)
_KEY_NAME_RE = re.compile(r"[A-Za-z0-9-]{1,127}")
_KEY_VERSION_RE = re.compile(r"[0-9a-fA-F]{32}")
# Refresh a cached access token this long before it expires.
_TOKEN_SKEW_SECONDS = 300


@dataclass(frozen=True)
class AzureKeyId:
    host: str
    name: str
    version: str | None

    @property
    def suffix(self) -> str:
        return self.host.split(".", 1)[1]

    @property
    def scope(self) -> str:
        """OAuth scope for this cloud's Key Vault / Managed HSM data plane."""
        return f"https://{self.suffix}/.default"

    @property
    def url(self) -> str:
        base = f"https://{self.host}/keys/{self.name}"
        return f"{base}/{self.version}" if self.version else base


def parse_azure_key_id(value: str, *, require_version: bool = False) -> AzureKeyId:
    """Validate a Key Vault key id. Raises KeyProviderNotConfigured, naming
    what is wrong but not echoing the value."""
    try:
        parts = urlsplit(value.strip())
        port = parts.port  # raises ValueError for a malformed port
    except ValueError:
        parts, port = urlsplit(""), -1
    host = (parts.hostname or "").lower()
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.query
        or parts.fragment
        or not _HOST_RE.fullmatch(host)
        # hostname drops a trailing dot and lowercases; the netloc must be
        # exactly the host, so nothing about it is ambiguous.
        or parts.netloc.lower() != host
    ):
        raise KeyProviderNotConfigured(
            "Azure Key Vault key id must be https://<vault>.<suffix>/keys/<name>"
            f"[/<version>] with a suffix in {', '.join(AZURE_HOST_SUFFIXES)}, "
            "and no port, query or credentials"
        )
    segments = parts.path.split("/")
    if (
        len(segments) not in (3, 4)
        or segments[0] != ""
        or segments[1] != "keys"
        or not _KEY_NAME_RE.fullmatch(segments[2])
        or (len(segments) == 4 and not _KEY_VERSION_RE.fullmatch(segments[3]))
    ):
        raise KeyProviderNotConfigured(
            "Azure Key Vault key id path must be /keys/<name>[/<32 hex version>]"
        )
    version = segments[3].lower() if len(segments) == 4 else None
    if require_version and version is None:
        raise KeyProviderNotConfigured("stored Azure Key Vault key id has no version")
    return AzureKeyId(host=host, name=segments[2], version=version)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def default_azure_credential() -> Any:
    """Workload identity when AZURE_FEDERATED_TOKEN_FILE is set (AKS
    workload identity webhook), else managed identity (AZURE_CLIENT_ID
    picks a user-assigned identity). Never a client secret."""
    try:
        from azure.identity import (  # type: ignore[import-not-found]
            ManagedIdentityCredential,
            WorkloadIdentityCredential,
        )
    except ImportError:
        raise KeyProviderNotConfigured(
            "PS_CREDENTIAL_KEK_PROVIDER=azure-keyvault needs the azure-identity "
            'package: pip install "prompt-shields-collector[azure-keyvault]"'
        ) from None
    if os.environ.get("AZURE_FEDERATED_TOKEN_FILE"):
        return WorkloadIdentityCredential()
    client_id = os.environ.get("AZURE_CLIENT_ID") or None
    return ManagedIdentityCredential(client_id=client_id)


class AzureKeyVaultProvider:
    """wrapKey / unwrapKey against Azure Key Vault or Managed HSM."""

    name = AZURE_KEYVAULT

    def __init__(
        self,
        key_id: str,
        *,
        algorithm: str = "RSA-OAEP-256",
        timeout: float = 5.0,
        credential: Any = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if algorithm not in WRAP_ALGORITHMS:
            raise KeyProviderNotConfigured(
                f"PS_AZURE_KEYVAULT_WRAP_ALGORITHM must be one of {', '.join(WRAP_ALGORITHMS)}"
            )
        self.key = parse_azure_key_id(key_id) if key_id else None
        self.algorithm = algorithm
        self.timeout = timeout
        self._credential = credential
        self._transport = transport
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    def _configured_key(self) -> AzureKeyId:
        if self.key is None:
            raise KeyProviderNotConfigured("PS_AZURE_KEYVAULT_KEY_ID is not set")
        return self.key

    async def _token(self, scope: str) -> str:
        async with self._lock:
            cached = self._tokens.get(scope)
            if cached and cached[1] - _TOKEN_SKEW_SECONDS > time.time():
                return cached[0]
            if self._credential is None:
                self._credential = default_azure_credential()
            try:
                # azure-identity's credentials are synchronous: keep the
                # event loop free while one talks to the identity endpoint.
                token = await asyncio.to_thread(self._credential.get_token, scope)
            except Exception as exc:  # noqa: BLE001 - azure-identity raises many types
                raise KeyProviderError(
                    f"azure-keyvault: could not get an access token ({type(exc).__name__})"
                ) from None
            self._tokens[scope] = (token.token, float(token.expires_on))
            return token.token

    async def _call(self, key: AzureKeyId, op: str, body: dict | None) -> dict:
        url = f"{key.url}/{op}" if op else key.url
        token = await self._token(key.scope)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self._transport, follow_redirects=False
            ) as client:
                if body is None:
                    resp = await client.get(
                        url, params={"api-version": API_VERSION}, headers=headers
                    )
                else:
                    resp = await client.post(
                        url, params={"api-version": API_VERSION}, headers=headers, json=body
                    )
        except httpx.HTTPError as exc:
            raise KeyProviderError(
                f"azure-keyvault: {op or 'get'} failed ({type(exc).__name__})"
            ) from None
        if resp.status_code != 200:
            code = ""
            try:
                code = str(resp.json().get("error", {}).get("code", ""))[:64]
            except Exception:  # noqa: BLE001 - any non-JSON error body
                code = ""
            if resp.status_code == 401:
                self._tokens.pop(key.scope, None)
            raise KeyProviderError(
                f"azure-keyvault: {op or 'get'} returned HTTP {resp.status_code}"
                + (f" ({code})" if re.fullmatch(r"[A-Za-z0-9_.-]+", code) else "")
            )
        try:
            data = resp.json()
        except ValueError:
            raise KeyProviderError(f"azure-keyvault: {op or 'get'} returned invalid JSON") from None
        if not isinstance(data, dict):
            raise KeyProviderError(f"azure-keyvault: {op or 'get'} returned invalid JSON")
        return data

    def _returned_key(self, requested: AzureKeyId, kid: object) -> AzureKeyId:
        """The versioned key id Key Vault reports, which must be the key we
        asked for (same host and name)."""
        try:
            got = parse_azure_key_id(str(kid), require_version=True)
        except KeyProviderNotConfigured:
            raise KeyProviderError("azure-keyvault: response has an invalid key id") from None
        if got.host != requested.host or got.name.lower() != requested.name.lower():
            raise KeyProviderError("azure-keyvault: response names another key")
        if requested.version and got.version != requested.version:
            raise KeyProviderError("azure-keyvault: response names another key version")
        return got

    def _ref(self, key: AzureKeyId) -> str:
        return f"{AZURE_KEYVAULT}:{self.algorithm}:{key.url}"

    async def current_ref(self) -> str:
        key = self._configured_key()
        if key.version:
            return self._ref(key)
        data = await self._call(key, "", None)
        return self._ref(self._returned_key(key, (data.get("key") or {}).get("kid")))

    async def wrap(self, dek: bytes, context: bytes) -> WrappedKey:
        key = self._configured_key()
        data = await self._call(key, "wrapkey", {"alg": self.algorithm, "value": _b64url(dek)})
        got = self._returned_key(key, data.get("kid"))
        value = data.get("value")
        if not isinstance(value, str) or not value:
            raise KeyProviderError("azure-keyvault: wrapkey returned no value")
        try:
            wrapped = _unb64url(value)
        except ValueError:
            raise KeyProviderError("azure-keyvault: wrapkey returned invalid base64") from None
        return WrappedKey(self._ref(got), wrapped)

    async def unwrap(self, kek_ref: str, wrapped: bytes, context: bytes) -> bytes:
        prefix, _, rest = kek_ref.partition(":")
        algorithm, _, key_url = rest.partition(":")
        if prefix != AZURE_KEYVAULT or algorithm not in WRAP_ALGORITHMS:
            raise KeyProviderError("not an azure-keyvault kek_ref")
        try:
            key = parse_azure_key_id(key_url, require_version=True)
        except KeyProviderNotConfigured:
            raise KeyProviderError("stored azure-keyvault kek_ref is invalid") from None
        data = await self._call(key, "unwrapkey", {"alg": algorithm, "value": _b64url(wrapped)})
        self._returned_key(key, data.get("kid"))
        value = data.get("value")
        try:
            dek = _unb64url(value) if isinstance(value, str) else b""
        except ValueError:
            dek = b""
        if len(dek) != DEK_BYTES:
            raise KeyProviderError("azure-keyvault: unwrapkey returned no data key")
        return dek


# ──────────────────────────────────────────────────────────────────────
# Registry
# ──────────────────────────────────────────────────────────────────────

_instances: dict[tuple, KeyProvider] = {}
_overrides: dict[str, KeyProvider] = {}


def use_provider(provider: KeyProvider | None, name: str | None = None) -> None:
    """Tests: serve ``provider`` for its name (``None`` removes the override
    for ``name``)."""
    if provider is None:
        _overrides.pop(name or "", None)
    else:
        _overrides[provider.name] = provider


def reset() -> None:
    _instances.clear()
    _overrides.clear()


def _build(name: str) -> KeyProvider:
    if name == LOCAL:
        return LocalKeyProvider()
    if name == AZURE_KEYVAULT:
        cache_key = (
            name,
            settings.azure_keyvault_key_id,
            settings.azure_keyvault_wrap_algorithm,
            settings.azure_keyvault_timeout_seconds,
        )
        provider = _instances.get(cache_key)
        if provider is None:
            provider = AzureKeyVaultProvider(
                settings.azure_keyvault_key_id,
                algorithm=settings.azure_keyvault_wrap_algorithm,
                timeout=settings.azure_keyvault_timeout_seconds,
            )
            _instances[cache_key] = provider
        return provider
    raise KeyProviderNotConfigured(
        f"PS_CREDENTIAL_KEK_PROVIDER must be one of {', '.join(PROVIDERS)}"
    )


def provider_named(name: str) -> KeyProvider:
    return _overrides.get(name) or _build(name)


def active_provider() -> KeyProvider:
    """The provider new data keys are wrapped with."""
    return provider_named(settings.credential_kek_provider.strip().lower())


def provider_for_ref(kek_ref: str) -> KeyProvider:
    """The provider that can unwrap a data key carrying ``kek_ref``, which
    need not be the active one (data keys wrapped before a provider
    change keep working until rewrapped)."""
    name = ref_provider(kek_ref)
    if name not in PROVIDERS:
        raise KeyProviderError("unknown kek_ref provider")
    return provider_named(name)
