"""KEK providers for the inference credential vault (#342).

The Azure Key Vault provider runs against ``FakeKeyVault``, an in-process
mock of the Key Vault REST API (7.4 ``wrapkey`` / ``unwrapkey`` / get key,
request and response shapes as documented) on an ``httpx.MockTransport``
with real RSA-OAEP-256 keys: no network, no Azure SDK. Also covers the
vault URL allow-list (no arbitrary egress) and the local keyring provider.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from types import SimpleNamespace

import httpx
import pytest
from collector import kms
from collector.config import settings
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

VAULT = "https://ps-kek-test.vault.azure.net"
KEY_URL = f"{VAULT}/keys/credential-kek"
V1 = "1" * 32
V2 = "2" * 32
K1 = "a" * 40
K2 = "b" * 40

_OAEP = {
    "RSA-OAEP-256": padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
    ),
    "RSA-OAEP": padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None),
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class FakeKeyVault:
    """Key Vault data plane for one vault: keys with RSA versions."""

    def __init__(self, host: str = "ps-kek-test.vault.azure.net", token: str = "tok-1") -> None:
        self.host = host
        self.token = token
        self.keys: dict[str, dict[str, rsa.RSAPrivateKey]] = {}
        self.current: dict[str, str] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: tuple[int, str] | None = None
        self.reply_kid: str | None = None  # override the kid in responses

    def add_version(self, name: str, version: str) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys.setdefault(name, {})[version] = key
        self.current[name] = version

    def kid(self, name: str, version: str) -> str:
        return f"https://{self.host}/keys/{name}/{version}"

    def _error(self, status: int, code: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": "nope"}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.scheme == "https"
        assert request.url.params.get("api-version") == kms.API_VERSION
        if request.url.host != self.host:
            return self._error(404, "VaultNotFound")
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return self._error(401, "Unauthorized")
        if self.fail_with:
            return self._error(*self.fail_with)
        parts = request.url.path.strip("/").split("/")
        if parts[0] != "keys" or parts[1] not in self.keys:
            return self._error(404, "KeyNotFound")
        name = parts[1]
        version = parts[2] if len(parts) > 2 and parts[2] not in ("wrapkey", "unwrapkey") else None
        op = parts[-1] if parts[-1] in ("wrapkey", "unwrapkey") else None
        version = version or self.current[name]
        key = self.keys[name].get(version)
        if key is None:
            return self._error(404, "KeyNotFound")
        kid = self.reply_kid or self.kid(name, version)
        if op is None:
            assert request.method == "GET"
            return httpx.Response(200, json={"key": {"kid": kid, "kty": "RSA"}, "attributes": {}})
        assert request.method == "POST"
        body = json.loads(request.content)
        assert set(body) == {"alg", "value"}
        pad = _OAEP.get(body["alg"])
        if pad is None:
            return self._error(400, "BadParameter")
        if op == "wrapkey":
            value = key.public_key().encrypt(_unb64(body["value"]), pad)
        else:
            try:
                value = key.decrypt(_unb64(body["value"]), pad)
            except ValueError:
                return self._error(400, "BadParameter")
        return httpx.Response(200, json={"kid": kid, "value": _b64(value)})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class FakeCredential:
    def __init__(self, token: str = "tok-1") -> None:
        self.token = token
        self.scopes: list[str] = []

    def get_token(self, scope: str):
        self.scopes.append(scope)
        return SimpleNamespace(token=self.token, expires_on=4_000_000_000)


def azure_provider(fake: FakeKeyVault, key_id: str = KEY_URL, **kw) -> kms.AzureKeyVaultProvider:
    return kms.AzureKeyVaultProvider(
        key_id, credential=kw.pop("credential", FakeCredential()), transport=fake.transport(), **kw
    )


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(settings, "credential_kek_keys", f"k1:{K1}")
    monkeypatch.setattr(settings, "credential_kek_provider", "local")
    kms.reset()
    yield
    kms.reset()


# ── vault URL allow-list ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "url,scope",
    [
        (KEY_URL, "https://vault.azure.net/.default"),
        (f"{KEY_URL}/{V1}", "https://vault.azure.net/.default"),
        ("https://kv-1.vault.azure.cn/keys/k", "https://vault.azure.cn/.default"),
        ("https://kv-1.vault.usgovcloudapi.net/keys/k", "https://vault.usgovcloudapi.net/.default"),
        ("https://hsm1.managedhsm.azure.net/keys/k", "https://managedhsm.azure.net/.default"),
        ("https://PS-KEK-TEST.Vault.Azure.Net/keys/k", "https://vault.azure.net/.default"),
    ],
)
def test_allowed_key_ids(url, scope):
    key = kms.parse_azure_key_id(url)
    assert key.scope == scope
    assert key.url.startswith("https://") and key.host == key.host.lower()


@pytest.mark.parametrize(
    "url",
    [
        "http://ps-kek-test.vault.azure.net/keys/k",
        "https://evil.example.com/keys/k",
        "https://ps-kek-test.vault.azure.net.evil.com/keys/k",
        "https://vault.azure.net/keys/k",  # no vault name
        "https://a.vault.azure.net/keys/k",  # name too short
        "https://-bad.vault.azure.net/keys/k",
        "https://bad--name.vault.azure.net/keys/k",
        "https://ps-kek-test.vault.azure.net:8443/keys/k",
        "https://user:pw@ps-kek-test.vault.azure.net/keys/k",
        "https://ps-kek-test.vault.azure.net@evil.com/keys/k",
        "https://ps-kek-test.vault.azure.net./keys/k",
        "https://ps-kek-test.vault.azure.net/keys/k?x=1",
        "https://ps-kek-test.vault.azure.net/keys/k#f",
        "https://ps-kek-test.vault.azure.net/secrets/k",
        "https://ps-kek-test.vault.azure.net/keys/k/notaversion",
        "https://ps-kek-test.vault.azure.net/keys/k/" + V1 + "/wrapkey",
        "https://ps-kek-test.vault.azure.net/keys/../x",
        "https://169.254.169.254/keys/k",
        "https://ps-kek-test.vault.azure.net:abc/keys/k",
        "https://[::1/keys/k",
        "",
    ],
)
def test_refused_key_ids(url):
    with pytest.raises(kms.KeyProviderNotConfigured) as err:
        kms.parse_azure_key_id(url)
    if url:
        assert url not in str(err.value)


def test_provider_refuses_a_misconfigured_vault_url_before_any_call():
    with pytest.raises(kms.KeyProviderNotConfigured):
        kms.AzureKeyVaultProvider("https://attacker.example/keys/k")
    with pytest.raises(kms.KeyProviderNotConfigured):
        kms.AzureKeyVaultProvider(KEY_URL, algorithm="RSA1_5")


# ── Azure Key Vault against the mocked API ────────────────────────────


async def test_wrap_unwrap_round_trip_sends_only_the_data_key():
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    cred = FakeCredential()
    provider = azure_provider(fake, credential=cred)
    dek = secrets.token_bytes(32)
    wrapped = await provider.wrap(dek, b"ctx")
    assert wrapped.kek_ref == f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V1}"
    assert dek not in wrapped.wrapped and len(wrapped.wrapped) == 256
    wrap_req = fake.requests[-1]
    assert wrap_req.url.path == "/keys/credential-kek/wrapkey"
    sent = json.loads(wrap_req.content)
    assert sent["alg"] == "RSA-OAEP-256" and _unb64(sent["value"]) == dek

    assert await provider.unwrap(wrapped.kek_ref, wrapped.wrapped, b"ctx") == dek
    unwrap_req = fake.requests[-1]
    # Unwrap goes to the exact key version that wrapped.
    assert unwrap_req.url.path == f"/keys/credential-kek/{V1}/unwrapkey"
    # One token, for the Key Vault scope, reused.
    assert cred.scopes == ["https://vault.azure.net/.default"]


async def test_current_ref_resolves_the_current_version():
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    provider = azure_provider(fake)
    assert await provider.current_ref() == f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V1}"
    fake.add_version("credential-kek", V2)
    assert await provider.current_ref() == f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V2}"
    pinned = azure_provider(fake, f"{KEY_URL}/{V1}")
    n = len(fake.requests)
    assert await pinned.current_ref() == f"azure-keyvault:RSA-OAEP-256:{KEY_URL}/{V1}"
    assert len(fake.requests) == n  # a pinned version needs no call


async def test_old_versions_still_unwrap_after_key_rotation():
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    provider = azure_provider(fake)
    dek = secrets.token_bytes(32)
    old = await provider.wrap(dek, b"")
    fake.add_version("credential-kek", V2)
    new = await provider.wrap(dek, b"")
    assert old.kek_ref.endswith(V1) and new.kek_ref.endswith(V2)
    assert await provider.unwrap(old.kek_ref, old.wrapped, b"") == dek
    assert await provider.unwrap(new.kek_ref, new.wrapped, b"") == dek


async def test_errors_name_status_and_code_but_no_key_material():
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    provider = azure_provider(fake)
    dek = secrets.token_bytes(32)
    wrapped = await provider.wrap(dek, b"")
    fake.fail_with = (403, "Forbidden")
    with pytest.raises(kms.KeyProviderError) as err:
        await provider.unwrap(wrapped.kek_ref, wrapped.wrapped, b"")
    msg = str(err.value)
    assert "403" in msg and "Forbidden" in msg
    for secret in (_b64(dek), _b64(wrapped.wrapped), "tok-1"):
        assert secret not in msg
    fake.fail_with = None
    # Tampered wrapped key: Key Vault refuses, we report it without data.
    with pytest.raises(kms.KeyProviderError):
        await provider.unwrap(wrapped.kek_ref, b"\x00" * 256, b"")


async def test_401_drops_the_cached_token():
    fake = FakeKeyVault(token="tok-2")
    fake.add_version("credential-kek", V1)
    cred = FakeCredential("tok-1")
    provider = azure_provider(fake, credential=cred)
    with pytest.raises(kms.KeyProviderError, match="401"):
        await provider.wrap(secrets.token_bytes(32), b"")
    cred.token = "tok-2"
    await provider.wrap(secrets.token_bytes(32), b"")
    assert len(cred.scopes) == 2


async def test_a_response_naming_another_key_is_refused():
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    provider = azure_provider(fake)
    fake.reply_kid = f"https://other-vault.vault.azure.net/keys/credential-kek/{V1}"
    with pytest.raises(kms.KeyProviderError, match="another key"):
        await provider.wrap(secrets.token_bytes(32), b"")
    fake.reply_kid = f"https://evil.example.com/keys/credential-kek/{V1}"
    with pytest.raises(kms.KeyProviderError, match="invalid key id"):
        await provider.wrap(secrets.token_bytes(32), b"")


@pytest.mark.parametrize(
    "ref",
    [
        f"azure-keyvault:RSA-OAEP-256:https://evil.example.com/keys/k/{V1}",
        f"azure-keyvault:RSA-OAEP-256:http://ps-kek-test.vault.azure.net/keys/k/{V1}",
        "azure-keyvault:RSA-OAEP-256:https://ps-kek-test.vault.azure.net/keys/k",  # no version
        f"azure-keyvault:RSA1_5:{KEY_URL}/{V1}",
        f"local:{KEY_URL}",
    ],
)
async def test_a_tampered_kek_ref_never_leaves_the_process(ref):
    fake = FakeKeyVault()
    fake.add_version("credential-kek", V1)
    provider = azure_provider(fake)
    with pytest.raises(kms.KeyProviderError):
        await provider.unwrap(ref, b"x" * 256, b"")
    assert fake.requests == []


async def test_network_errors_are_key_provider_errors():
    def boom(request):
        raise httpx.ConnectError("no route", request=request)

    provider = kms.AzureKeyVaultProvider(
        KEY_URL, credential=FakeCredential(), transport=httpx.MockTransport(boom)
    )
    with pytest.raises(kms.KeyProviderError, match="ConnectError"):
        await provider.wrap(secrets.token_bytes(32), b"")


async def test_unset_key_id_and_missing_azure_identity(monkeypatch):
    with pytest.raises(kms.KeyProviderNotConfigured, match="PS_AZURE_KEYVAULT_KEY_ID"):
        await kms.AzureKeyVaultProvider("").wrap(b"x" * 32, b"")
    monkeypatch.setitem(sys.modules, "azure.identity", None)  # import fails
    provider = kms.AzureKeyVaultProvider(KEY_URL, transport=FakeKeyVault().transport())
    with pytest.raises(kms.KeyProviderNotConfigured, match="azure-keyvault"):
        await provider.wrap(b"x" * 32, b"")


def test_registry_builds_the_configured_provider(monkeypatch):
    assert isinstance(kms.active_provider(), kms.LocalKeyProvider)
    monkeypatch.setattr(settings, "credential_kek_provider", "azure-keyvault")
    monkeypatch.setattr(settings, "azure_keyvault_key_id", KEY_URL)
    first = kms.active_provider()
    assert isinstance(first, kms.AzureKeyVaultProvider)
    assert kms.active_provider() is first  # token cache survives
    monkeypatch.setattr(settings, "credential_kek_provider", "aws-kms")
    with pytest.raises(kms.KeyProviderNotConfigured):
        kms.active_provider()
    with pytest.raises(kms.KeyProviderError):
        kms.provider_for_ref("aws-kms:arn")
    assert isinstance(kms.provider_for_ref("local:k1"), kms.LocalKeyProvider)


# ── local keyring provider ────────────────────────────────────────────


async def test_local_round_trip_is_bound_to_context_and_key_id(monkeypatch):
    provider = kms.LocalKeyProvider()
    dek = secrets.token_bytes(32)
    wrapped = await provider.wrap(dek, b"ctx-a")
    assert wrapped.kek_ref == "local:k1"
    assert await provider.unwrap("local:k1", wrapped.wrapped, b"ctx-a") == dek
    with pytest.raises(kms.KeyProviderError):
        await provider.unwrap("local:k1", wrapped.wrapped, b"ctx-b")
    # Rotation: k2 wraps, k1 still unwraps; without k1 it is reported, not leaked.
    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2},k1:{K1}")
    assert await provider.current_ref() == "local:k2"
    assert await provider.unwrap("local:k1", wrapped.wrapped, b"ctx-a") == dek
    monkeypatch.setattr(settings, "credential_kek_keys", f"k2:{K2}")
    with pytest.raises(kms.KeyProviderNotConfigured, match="'k1'") as err:
        await provider.unwrap("local:k1", wrapped.wrapped, b"ctx-a")
    assert K2 not in str(err.value)


async def test_local_keyring_is_not_the_webhook_keyring(monkeypatch):
    monkeypatch.setattr(settings, "credential_kek_keys", "")
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    with pytest.raises(kms.KeyProviderNotConfigured, match="PS_CREDENTIAL_KEK_KEYS"):
        await kms.LocalKeyProvider().wrap(b"x" * 32, b"")
    monkeypatch.setattr(settings, "credential_kek_keys", "k1:short")
    with pytest.raises(kms.KeyProviderNotConfigured) as err:
        await kms.LocalKeyProvider().wrap(b"x" * 32, b"")
    assert "short" not in str(err.value)
