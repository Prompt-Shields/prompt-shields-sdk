"""A fake OpenID Connect identity provider for the SSO tests (#339).

Serves discovery, a JWKS and a token endpoint through an httpx
MockTransport plugged into collector.oidc.fetcher, signs ID tokens with
real RSA / EC / Ed25519 keys, and checks PKCE on the code exchange. The
resolver returns a public address, so the SSRF guard is exercised as in
production.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
from collector import oidc
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

ISSUER = "https://idp.example.com"
CLIENT_ID = "ps-admin"
PUBLIC_IP = "93.184.216.34"

_H = {"256": hashes.SHA256, "384": hashes.SHA384, "512": hashes.SHA512}


def _int_b64(n: int, size: int | None = None) -> str:
    raw = n.to_bytes(size or (n.bit_length() + 7) // 8, "big")
    return oidc.b64url_encode(raw)


@dataclass
class Key:
    kid: str
    alg: str
    private: object

    def jwk(self) -> dict:
        pub = self.private.public_key()
        if self.alg.startswith(("RS", "PS")):
            nums = pub.public_numbers()
            return {"kty": "RSA", "kid": self.kid, "use": "sig", "n": _int_b64(nums.n), "e": _int_b64(nums.e)}
        if self.alg.startswith("ES"):
            crv, _, size = oidc._EC_CURVES[self.alg]
            nums = pub.public_numbers()
            return {
                "kty": "EC",
                "kid": self.kid,
                "crv": crv,
                "x": _int_b64(nums.x, size),
                "y": _int_b64(nums.y, size),
            }
        from cryptography.hazmat.primitives import serialization

        raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return {"kty": "OKP", "kid": self.kid, "crv": "Ed25519", "x": oidc.b64url_encode(raw)}

    def sign(self, signing_input: bytes) -> bytes:
        alg = self.alg
        if alg.startswith("RS"):
            return self.private.sign(signing_input, padding.PKCS1v15(), _H[alg[2:]]())
        if alg.startswith("PS"):
            h = _H[alg[2:]]
            return self.private.sign(
                signing_input, padding.PSS(mgf=padding.MGF1(h()), salt_length=h.digest_size), h()
            )
        if alg.startswith("ES"):
            size = oidc._EC_CURVES[alg][2]
            der = self.private.sign(signing_input, ec.ECDSA(_H[alg[2:]]()))
            r, s = decode_dss_signature(der)
            return r.to_bytes(size, "big") + s.to_bytes(size, "big")
        return self.private.sign(signing_input)


def make_key(alg: str = "RS256", kid: str = "k1", bits: int = 2048) -> Key:
    if alg.startswith(("RS", "PS")):
        return Key(kid, alg, rsa.generate_private_key(public_exponent=65537, key_size=bits))
    if alg.startswith("ES"):
        return Key(kid, alg, ec.generate_private_key(oidc._EC_CURVES[alg][1]()))
    return Key(kid, alg, Ed25519PrivateKey.generate())


def encode(header: dict, claims: dict, key: Key | None = None, signature: bytes | None = None) -> str:
    h = oidc.b64url_encode(json.dumps(header).encode())
    p = oidc.b64url_encode(json.dumps(claims).encode())
    signing_input = f"{h}.{p}".encode()
    sig = signature if signature is not None else key.sign(signing_input)
    return f"{h}.{p}.{oidc.b64url_encode(sig)}"


def claims(**over) -> dict:
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-1",
        "iat": now,
        "exp": now + 300,
        "nonce": "n-1",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


@dataclass
class FakeIdp:
    keys: list[Key] = field(default_factory=lambda: [make_key()])
    issuer: str = ISSUER
    jwks_cache_control: str = "max-age=600"
    jwks_status: int = 200
    discovery_overrides: dict = field(default_factory=dict)
    # The claims the next code exchange puts in the ID token (sub etc.).
    id_token_claims: dict = field(default_factory=dict)
    signing_key: Key | None = None
    # Captured from the authorization redirect: nonce and PKCE challenge.
    nonce: str | None = None
    challenge: str | None = None
    requests: list[httpx.Request] = field(default_factory=list)
    token_requests: list[dict] = field(default_factory=list)
    address: str = PUBLIC_IP

    def discovery(self) -> dict:
        doc = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{ISSUER}/authorize",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/jwks",
        }
        doc.update(self.discovery_overrides)
        return doc

    def jwks_hits(self) -> int:
        return sum(1 for r in self.requests if r.url.path == "/jwks")

    def capture_authorize(self, location: str) -> dict:
        url = httpx.URL(location)
        params = dict(url.params)
        self.nonce = params.get("nonce")
        self.challenge = params.get("code_challenge")
        return params

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if path == "/jwks":
            if self.jwks_status != 200:
                return httpx.Response(self.jwks_status)
            return httpx.Response(
                200,
                json={"keys": [k.jwk() for k in self.keys]},
                headers={"Cache-Control": self.jwks_cache_control},
            )
        if path == "/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            form["_authorization"] = request.headers.get("authorization")
            self.token_requests.append(form)
            if form.get("grant_type") != "authorization_code" or form.get("code") != "good-code":
                return httpx.Response(400, json={"error": "invalid_grant"})
            if oidc.pkce_challenge(form.get("code_verifier", "")) != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            key = self.signing_key or self.keys[0]
            token = encode(
                {"alg": key.alg, "kid": key.kid, "typ": "JWT"},
                claims(nonce=self.nonce, **self.id_token_claims),
                key,
            )
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": token})
        return httpx.Response(404)

    def install(self, monkeypatch) -> None:
        async def resolver(host: str, port: int) -> list[str]:
            return [self.address]

        monkeypatch.setattr(oidc, "fetcher", oidc.Fetcher(httpx.MockTransport(self.handler), resolver))
        monkeypatch.setattr(oidc, "cache", oidc.Cache())
