"""OpenID Connect ID token verification for admin API login (#339).

Follows the gateway's approach (gateway/src/middlewares/psOidc/jwks.ts):

  - Asymmetric JWS algorithms only (RS*, PS*, ES*, and EdDSA when a tenant
    lists it). ``HS*`` (shared secret) and ``none`` are never accepted, and
    a JWK of type ``oct`` is never used. The algorithm comes from the token
    header but must be in the tenant's list and match the key's type (and
    the key's own ``alg`` when it has one), so no algorithm confusion.
    Signatures are checked with ``cryptography`` directly.
  - Discovery documents and key sets are fetched with the SSRF-safe egress
    client (collector.connectors.GuardedClient: https only, every resolved
    address checked, connection pinned to the checked address, redirects
    never followed, no proxy from the environment), with a timeout and a
    size cap. The discovery document's ``issuer`` must equal the
    configured issuer exactly, and every endpoint it names must be https.
  - Keys are cached per ``jwks_uri`` for the response's
    ``Cache-Control: max-age``, clamped to MIN_TTL..MAX_TTL. A token whose
    ``kid`` is not cached triggers a refresh (key rotation), but refreshes
    happen at most once per REFRESH_COOLDOWN per key set, so tokens with
    random ``kid``s cannot turn the collector against the IdP. When a
    refresh fails the previous keys keep being used.
  - Claims: ``iss`` equals the issuer; ``aud`` contains the audience (and
    with several audiences, ``azp`` is the client id); ``exp`` is required
    and in the future, ``nbf`` (if present) and ``iat`` (required) not in
    the future, each within the clock skew; ``iat`` no older than
    MAX_TOKEN_AGE; ``nonce`` equals the login's nonce.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from collector import connectors
from collector.config import settings

logger = logging.getLogger("prompt_shields.collector.oidc")

ASYMMETRIC_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
# Like the gateway's DEFAULT_ALGORITHMS: every asymmetric algorithm but EdDSA.
DEFAULT_ALGORITHMS = tuple(a for a in ASYMMETRIC_ALGORITHMS if a != "EdDSA")

CLOCK_SKEW_SECONDS = 60
MAX_TOKEN_AGE_SECONDS = 600
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 3600
DEFAULT_TTL_SECONDS = 600
REFRESH_COOLDOWN_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 5.0
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_TOKEN_LENGTH = 16 * 1024

_HASHES = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}
_EC_CURVES = {
    "ES256": ("P-256", ec.SECP256R1, 32),
    "ES384": ("P-384", ec.SECP384R1, 48),
    "ES512": ("P-521", ec.SECP521R1, 66),
}


class TokenError(Exception):
    """Why an ID token was refused (the reason goes to the audit trail)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class IdpError(Exception):
    """The identity provider could not be reached or answered badly."""


# ──────────────────────────────────────────────────────────────────────
# JWS
# ──────────────────────────────────────────────────────────────────────


def b64url_decode(value: str) -> bytes:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", value):
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64_int(value: Any) -> int:
    if not isinstance(value, str) or not value:
        raise ValueError("missing key parameter")
    return int.from_bytes(b64url_decode(value), "big")


def parse_jws(token: str) -> tuple[dict, dict, bytes, bytes]:
    """(header, payload, signing input, signature) of a compact JWS."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH:
        raise TokenError("malformed")
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("malformed")
    try:
        header = json.loads(b64url_decode(parts[0]))
        payload = json.loads(b64url_decode(parts[1]))
        signature = b64url_decode(parts[2])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise TokenError("malformed") from None
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenError("malformed")
    if "crit" in header:
        # No extension is understood, so a critical one must be refused.
        raise TokenError("malformed")
    return header, payload, f"{parts[0]}.{parts[1]}".encode("ascii"), signature


def _key_type_for(alg: str) -> str:
    if alg.startswith(("RS", "PS")):
        return "RSA"
    if alg.startswith("ES"):
        return "EC"
    return "OKP"


def public_key(jwk: dict, alg: str):
    """A ``cryptography`` public key for ``jwk`` usable with ``alg``.
    Raises ValueError when the key does not fit the algorithm."""
    kty = jwk.get("kty")
    if kty != _key_type_for(alg):
        raise ValueError("key type does not match the algorithm")
    if jwk.get("alg") not in (None, alg):
        raise ValueError("key is for another algorithm")
    if jwk.get("use") not in (None, "sig"):
        raise ValueError("key is not a signing key")
    if kty == "RSA":
        key = rsa.RSAPublicNumbers(_b64_int(jwk.get("e")), _b64_int(jwk.get("n"))).public_key()
        if key.key_size < 2048:
            raise ValueError("RSA key shorter than 2048 bits")
        return key
    if kty == "EC":
        crv, curve, size = _EC_CURVES[alg]
        if jwk.get("crv") != crv:
            raise ValueError("curve does not match the algorithm")
        x, y = b64url_decode(jwk.get("x") or ""), b64url_decode(jwk.get("y") or "")
        if len(x) != size or len(y) != size:
            raise ValueError("bad EC point")
        return ec.EllipticCurvePublicNumbers(
            int.from_bytes(x, "big"), int.from_bytes(y, "big"), curve()
        ).public_key()
    if jwk.get("crv") != "Ed25519":
        raise ValueError("only Ed25519 is supported for EdDSA")
    return Ed25519PublicKey.from_public_bytes(b64url_decode(jwk.get("x") or ""))


def verify_signature(alg: str, key, signing_input: bytes, signature: bytes) -> bool:
    try:
        if alg.startswith("RS"):
            key.verify(signature, signing_input, padding.PKCS1v15(), _HASHES[alg[2:]]())
        elif alg.startswith("PS"):
            h = _HASHES[alg[2:]]
            key.verify(
                signature,
                signing_input,
                padding.PSS(mgf=padding.MGF1(h()), salt_length=h.digest_size),
                h(),
            )
        elif alg.startswith("ES"):
            size = _EC_CURVES[alg][2]
            if len(signature) != 2 * size:
                return False
            r = int.from_bytes(signature[:size], "big")
            s = int.from_bytes(signature[size:], "big")
            key.verify(
                encode_dss_signature(r, s), signing_input, ec.ECDSA(_HASHES[alg[2:]]())
            )
        else:
            key.verify(signature, signing_input)
    except InvalidSignature:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Fetching documents through the SSRF-safe egress client
# ──────────────────────────────────────────────────────────────────────


def https_url(value: str) -> httpx.URL:
    """``value`` as an https URL without credentials or fragment, else
    ValueError."""
    url = httpx.URL(value)
    if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
        raise ValueError("must be an https URL without credentials or fragment")
    return url


@dataclass
class Fetcher:
    """Requests to identity providers. Tests swap ``transport`` (an httpx
    mock) and ``resolver`` (returns a public address)."""

    transport: httpx.AsyncBaseTransport | None = None
    resolver: connectors.Resolver = connectors.default_resolver

    def _client(self, url: httpx.URL) -> connectors.GuardedClient:
        origin = url.copy_with(path="/", query=None, fragment=None)
        return connectors.GuardedClient(
            str(origin),
            name="oidc",
            transport=self.transport,
            resolver=self.resolver,
            timeout=FETCH_TIMEOUT_SECONDS,
            max_retries=1,
            allow_private_network=settings.sso_idp_allow_private_network,
            # Never this host or its metadata service, even when private
            # networks are allowed.
            allow_local_network=False,
        )

    async def _send(self, method: str, url_text: str, **kwargs) -> tuple[Any, httpx.Headers]:
        try:
            url = https_url(url_text)
        except ValueError as exc:
            raise IdpError(f"refused URL: {exc}") from None
        try:
            async with self._client(url) as client:
                resp = await client.request(
                    method,
                    url.path,
                    what=url.host,
                    params=dict(url.params),
                    **kwargs,
                )
        except connectors.ConnectorError as exc:
            raise IdpError(str(exc)) from None
        if len(resp.content) > MAX_DOCUMENT_BYTES:
            raise IdpError(f"{url.host}: response larger than {MAX_DOCUMENT_BYTES} bytes")
        try:
            body = json.loads(resp.content)
        except ValueError:
            raise IdpError(f"{url.host}: response is not JSON") from None
        if not isinstance(body, dict):
            raise IdpError(f"{url.host}: response is not a JSON object")
        return body, resp.headers

    async def get_json(self, url: str) -> tuple[dict, httpx.Headers]:
        return await self._send("GET", url, headers={"Accept": "application/json"})

    async def post_form(self, url: str, form: dict, auth: tuple[str, str] | None) -> dict:
        body, _ = await self._send(
            "POST",
            url,
            data=form,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        return body


fetcher = Fetcher()


def ttl_from_cache_control(header: str | None) -> int:
    """max-age from Cache-Control, clamped; DEFAULT_TTL without one."""
    if not header:
        return DEFAULT_TTL_SECONDS
    h = header.lower()
    if re.search(r"(^|[,\s])(no-store|no-cache)\b", h):
        return MIN_TTL_SECONDS
    m = re.search(r'(?:^|[,\s])max-age\s*=\s*"?(\d+)"?', h)
    if not m:
        return DEFAULT_TTL_SECONDS
    return max(MIN_TTL_SECONDS, min(MAX_TTL_SECONDS, int(m.group(1))))


@dataclass
class Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


@dataclass
class _Entry:
    value: Any
    expires: float
    last_refresh: float | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Cache:
    """Discovery documents and key sets, per URL, in this process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._discovery: dict[str, _Entry] = {}
        self._jwks: dict[str, _Entry] = {}

    def clear(self) -> None:
        self._discovery.clear()
        self._jwks.clear()

    async def discovery(self, issuer: str) -> Discovery:
        now = self.clock()
        entry = self._discovery.get(issuer)
        if entry is not None and now < entry.expires:
            return entry.value
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        try:
            doc, headers = await fetcher.get_json(url)
        except IdpError:
            if entry is not None:
                logger.warning("OIDC discovery refresh failed; using the cached document")
                return entry.value
            raise
        if doc.get("issuer") != issuer:
            raise IdpError("discovery document names another issuer")
        values = {}
        for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            value = doc.get(name)
            try:
                https_url(value if isinstance(value, str) else "")
            except ValueError:
                raise IdpError(f"discovery document: {name} must be an https URL") from None
            values[name] = value
        found = Discovery(issuer=issuer, **values)
        self._discovery[issuer] = _Entry(
            found, now + ttl_from_cache_control(headers.get("cache-control")), now
        )
        return found

    async def _fetch_keys(self, jwks_uri: str) -> tuple[list[dict], int]:
        doc, headers = await fetcher.get_json(jwks_uri)
        keys = doc.get("keys")
        if not isinstance(keys, list):
            raise IdpError("JWKS has no keys array")
        usable = [k for k in keys if isinstance(k, dict) and k.get("kty") in ("RSA", "EC", "OKP")]
        return usable, ttl_from_cache_control(headers.get("cache-control"))

    async def keys(self, jwks_uri: str, *, want_kid: str | None) -> list[dict]:
        """The key set, refreshed when expired or when ``want_kid`` is not
        in it (key rotation), but at most once per REFRESH_COOLDOWN; when a
        refresh fails the cached keys keep being used."""
        entry = self._jwks.setdefault(jwks_uri, _Entry(None, 0.0, None))
        async with entry.lock:
            now = self.clock()
            if entry.value is not None and now < entry.expires and (
                want_kid is None or any(k.get("kid") == want_kid for k in entry.value)
            ):
                return entry.value
            if (
                entry.last_refresh is not None
                and now - entry.last_refresh < REFRESH_COOLDOWN_SECONDS
            ):
                if entry.value is None:
                    raise IdpError("JWKS unavailable (retrying after a cooldown)")
                return entry.value
            entry.last_refresh = now
            try:
                keys, ttl = await self._fetch_keys(jwks_uri)
            except IdpError:
                if entry.value is not None:
                    logger.warning("JWKS refresh failed; using the cached keys")
                    return entry.value
                raise
            entry.value = keys
            entry.expires = now + ttl
            return keys


cache = Cache()


# ──────────────────────────────────────────────────────────────────────
# ID token
# ──────────────────────────────────────────────────────────────────────


def select_key(keys: list[dict], kid: str | None, alg: str) -> dict:
    kty = _key_type_for(alg)
    if kid is not None:
        matches = [k for k in keys if k.get("kid") == kid]
    else:
        # No kid: only unambiguous when one key of the right type exists.
        matches = [k for k in keys if k.get("kty") == kty]
        if len(matches) > 1:
            raise TokenError("unknown_key")
    if not matches:
        raise TokenError("unknown_key")
    return matches[0]


@dataclass(frozen=True)
class Expected:
    issuer: str
    audience: str
    client_id: str
    algorithms: tuple[str, ...]
    nonce: str


async def verify_id_token(
    token: str, jwks_uri: str, expected: Expected, *, now: float | None = None
) -> dict:
    """The verified claims of ``token``, else TokenError."""
    header, claims, signing_input, signature = parse_jws(token)
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in expected.algorithms or alg not in ASYMMETRIC_ALGORITHMS:
        raise TokenError("disallowed_alg")
    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise TokenError("malformed")
    try:
        keys = await cache.keys(jwks_uri, want_kid=kid)
    except IdpError:
        raise TokenError("jwks_unavailable") from None
    jwk = select_key(keys, kid, alg)
    try:
        key = public_key(jwk, alg)
    except (ValueError, KeyError, binascii.Error):
        raise TokenError("unknown_key") from None
    if not verify_signature(alg, key, signing_input, signature):
        raise TokenError("bad_signature")
    check_claims(claims, expected, now=now)
    return claims


def _num(claims: dict, name: str, required: bool) -> float | None:
    value = claims.get(name)
    if value is None:
        if required:
            raise TokenError("invalid_claims")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TokenError("invalid_claims")
    return float(value)


def check_claims(claims: dict, expected: Expected, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    if claims.get("iss") != expected.issuer:
        raise TokenError("wrong_issuer")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud
    if not isinstance(audiences, list) or expected.audience not in audiences:
        raise TokenError("wrong_audience")
    azp = claims.get("azp")
    if (len(audiences) > 1 or azp is not None) and azp != expected.client_id:
        raise TokenError("wrong_audience")
    exp = _num(claims, "exp", True)
    iat = _num(claims, "iat", True)
    nbf = _num(claims, "nbf", False)
    if now >= exp + CLOCK_SKEW_SECONDS:
        raise TokenError("expired")
    if nbf is not None and now + CLOCK_SKEW_SECONDS < nbf:
        raise TokenError("not_yet_valid")
    if now + CLOCK_SKEW_SECONDS < iat:
        raise TokenError("not_yet_valid")
    if now - iat > MAX_TOKEN_AGE_SECONDS + CLOCK_SKEW_SECONDS:
        raise TokenError("expired")
    if not isinstance(claims.get("nonce"), str) or claims["nonce"] != expected.nonce:
        raise TokenError("wrong_nonce")


def pkce_challenge(verifier: str) -> str:
    """S256 code challenge (RFC 7636 §4.2)."""
    return b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())
