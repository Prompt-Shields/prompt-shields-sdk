"""ID token verification for admin API SSO (#339), against a fake IdP.

No database. Covers: every asymmetric algorithm, refusal of HS256 / none /
critical headers / weak or mismatched keys, issuer / audience / azp / exp /
nbf / iat / nonce checks, JWKS caching with Cache-Control, key rotation on
an unknown kid with a refresh cooldown, stale keys on a failed refresh, the
discovery document checks, and the SSRF guard (private addresses, no
redirects, https only).
"""

from __future__ import annotations

import hmac
import json
import time

import httpx
import pytest
from collector import oidc
from collector.config import settings

from tests.fake_idp import CLIENT_ID, ISSUER, FakeIdp, claims, encode, make_key

JWKS = f"{ISSUER}/jwks"


def expected(**over) -> oidc.Expected:
    base = {
        "issuer": ISSUER,
        "audience": CLIENT_ID,
        "client_id": CLIENT_ID,
        "algorithms": oidc.ASYMMETRIC_ALGORITHMS,
        "nonce": "n-1",
    }
    base.update(over)
    return oidc.Expected(**base)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdp()
    fake.install(monkeypatch)
    return fake


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(oidc, "cache", oidc.Cache(clock=c))
    return c


def token(key, **over) -> str:
    return encode({"alg": key.alg, "kid": key.kid}, claims(**over), key)


@pytest.mark.parametrize(
    "alg", ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"]
)
async def test_every_asymmetric_algorithm_verifies(idp, alg):
    key = make_key(alg, kid=f"k-{alg}")
    idp.keys = [key]
    got = await oidc.verify_id_token(token(key), JWKS, expected())
    assert got["sub"] == "user-1"


async def test_eddsa_is_refused_unless_listed(idp):
    key = make_key("EdDSA")
    idp.keys = [key]
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(key), JWKS, expected(algorithms=oidc.DEFAULT_ALGORITHMS))
    assert e.value.reason == "disallowed_alg"


async def test_hs256_and_none_are_never_accepted(idp):
    key = idp.keys[0]
    # HS256 "signed" with the RSA public key bytes (algorithm confusion).
    jwk = key.jwk()
    secret = jwk["n"].encode()
    h = encode({"alg": "HS256", "kid": key.kid}, claims(), signature=b"x")
    signing_input = h.rsplit(".", 1)[0].encode()
    forged = h.rsplit(".", 1)[0] + "." + oidc.b64url_encode(
        hmac.new(secret, signing_input, "sha256").digest()
    )
    for t in (forged, encode({"alg": "none"}, claims(), signature=b"")):
        with pytest.raises(oidc.TokenError) as e:
            await oidc.verify_id_token(t, JWKS, expected(algorithms=("HS256", "none", "RS256")))
        assert e.value.reason == "disallowed_alg"


async def test_oct_keys_are_ignored(idp):
    idp.keys = []

    def handler(request):
        idp.requests.append(request)
        return httpx.Response(200, json={"keys": [{"kty": "oct", "kid": "k1", "k": "c2VjcmV0"}]})

    oidc.fetcher.transport = httpx.MockTransport(handler)
    key = make_key()
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(key), JWKS, expected())
    assert e.value.reason == "unknown_key"


async def test_crit_header_and_garbage_are_malformed(idp):
    key = idp.keys[0]
    crit = encode({"alg": "RS256", "kid": key.kid, "crit": ["exp"]}, claims(), key)
    for t in (crit, "a.b", "not-a-token", "a.b.c", "x" * (oidc.MAX_TOKEN_LENGTH + 1)):
        with pytest.raises(oidc.TokenError) as e:
            await oidc.verify_id_token(t, JWKS, expected())
        assert e.value.reason == "malformed"


async def test_bad_signature_and_wrong_key(idp):
    key = idp.keys[0]
    other = make_key(kid=key.kid)  # same kid, different key
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(other), JWKS, expected())
    assert e.value.reason == "bad_signature"
    good = token(key)
    head, _, sig = good.split(".")
    tampered = claims(sub="admin")
    t2 = f"{head}.{oidc.b64url_encode(json.dumps(tampered).encode())}.{sig}"
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(t2, JWKS, expected())
    assert e.value.reason == "bad_signature"


async def test_key_must_match_the_algorithm(idp):
    rsa_key = idp.keys[0]
    # An RS256 key presented for an ES256 token: refused.
    ec_key = make_key("ES256", kid=rsa_key.kid)
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(ec_key), JWKS, expected())
    assert e.value.reason == "unknown_key"
    # A key pinned to RS384 cannot verify RS256.
    pinned = make_key("RS256", kid="pinned")
    jwk = pinned.jwk() | {"alg": "RS384"}
    assert pytest.raises(ValueError, oidc.public_key, jwk, "RS256")
    # Weak RSA keys are refused.
    weak = make_key("RS256", kid="weak", bits=1024)
    idp.keys = [weak]
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(weak), JWKS, expected())
    assert e.value.reason == "unknown_key"


@pytest.mark.parametrize(
    ("over", "exp_kw", "reason"),
    [
        ({"iss": "https://evil.example.com"}, {}, "wrong_issuer"),
        ({"aud": "someone-else"}, {}, "wrong_audience"),
        ({"aud": [CLIENT_ID, "other"]}, {}, "wrong_audience"),  # no azp
        ({"aud": [CLIENT_ID, "other"], "azp": "other"}, {}, "wrong_audience"),
        ({"exp": int(time.time()) - 120}, {}, "expired"),
        ({"exp": None}, {}, "invalid_claims"),
        ({"iat": None}, {}, "invalid_claims"),
        ({"iat": "yesterday"}, {}, "invalid_claims"),
        ({"nbf": int(time.time()) + 600}, {}, "not_yet_valid"),
        ({"iat": int(time.time()) + 600}, {}, "not_yet_valid"),
        ({"iat": int(time.time()) - 3600, "exp": int(time.time()) + 600}, {}, "expired"),
        ({"nonce": "other"}, {}, "wrong_nonce"),
        ({"nonce": None}, {}, "wrong_nonce"),
        ({}, {"audience": "api://admin"}, "wrong_audience"),
    ],
)
async def test_claim_checks(idp, over, exp_kw, reason):
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(idp.keys[0], **over), JWKS, expected(**exp_kw))
    assert e.value.reason == reason


async def test_multiple_audiences_with_azp_and_clock_skew_pass(idp):
    now = int(time.time())
    t = token(idp.keys[0], aud=[CLIENT_ID, "x"], azp=CLIENT_ID, exp=now - 30, nbf=now + 30)
    assert (await oidc.verify_id_token(t, JWKS, expected()))["sub"] == "user-1"


async def test_jwks_is_cached_for_max_age(idp, clock):
    idp.jwks_cache_control = "public, max-age=120"
    key = idp.keys[0]
    for _ in range(3):
        await oidc.verify_id_token(token(key), JWKS, expected())
    assert idp.jwks_hits() == 1
    clock.t += 121
    await oidc.verify_id_token(token(key), JWKS, expected())
    assert idp.jwks_hits() == 2


async def test_key_rotation_refreshes_once_per_cooldown(idp, clock):
    old = idp.keys[0]
    await oidc.verify_id_token(token(old), JWKS, expected())
    new = make_key(kid="k2")
    idp.keys = [old, new]
    clock.t += oidc.REFRESH_COOLDOWN_SECONDS + 1
    # Unknown kid: one refresh picks up the rotated key.
    await oidc.verify_id_token(token(new), JWKS, expected())
    assert idp.jwks_hits() == 2
    # Random kids within the cooldown do not hit the IdP again.
    for i in range(5):
        with pytest.raises(oidc.TokenError) as e:
            await oidc.verify_id_token(token(make_key(kid=f"rnd{i}")), JWKS, expected())
        assert e.value.reason == "unknown_key"
    assert idp.jwks_hits() == 2
    # The old key retired: once its entry expires, a refresh drops it.
    idp.keys = [new]
    clock.t += oidc.DEFAULT_TTL_SECONDS + 1
    with pytest.raises(oidc.TokenError):
        await oidc.verify_id_token(token(old), JWKS, expected())


async def test_failed_refresh_keeps_the_cached_keys(idp, clock):
    key = idp.keys[0]
    await oidc.verify_id_token(token(key), JWKS, expected())
    idp.jwks_status = 404
    clock.t += oidc.DEFAULT_TTL_SECONDS + 1
    assert (await oidc.verify_id_token(token(key), JWKS, expected()))["sub"] == "user-1"


async def test_jwks_unavailable(idp):
    idp.jwks_status = 404
    with pytest.raises(oidc.TokenError) as e:
        await oidc.verify_id_token(token(idp.keys[0]), JWKS, expected())
    assert e.value.reason == "jwks_unavailable"


def test_ttl_from_cache_control():
    assert oidc.ttl_from_cache_control(None) == oidc.DEFAULT_TTL_SECONDS
    assert oidc.ttl_from_cache_control("max-age=5") == oidc.MIN_TTL_SECONDS
    assert oidc.ttl_from_cache_control("max-age=999999") == oidc.MAX_TTL_SECONDS
    assert oidc.ttl_from_cache_control("no-store") == oidc.MIN_TTL_SECONDS
    assert oidc.ttl_from_cache_control("public, max-age=300") == 300


async def test_discovery_checks(idp):
    disc = await oidc.cache.discovery(ISSUER)
    assert disc.jwks_uri == JWKS
    idp.discovery_overrides = {"issuer": "https://evil.example.com"}
    oidc.cache.clear()
    with pytest.raises(oidc.IdpError):
        await oidc.cache.discovery(ISSUER)
    idp.discovery_overrides = {"jwks_uri": "http://idp.example.com/jwks"}
    with pytest.raises(oidc.IdpError):
        await oidc.cache.discovery(ISSUER)


async def test_private_addresses_are_refused(idp, monkeypatch):
    for address in ("10.0.0.5", "127.0.0.1", "169.254.169.254", "::ffff:10.0.0.1"):
        idp.address = address
        oidc.cache.clear()
        with pytest.raises(oidc.IdpError):
            await oidc.cache.discovery(ISSUER)
    # The opt-in admits a private network, never loopback or metadata.
    monkeypatch.setattr(settings, "sso_idp_allow_private_network", True)
    idp.address = "10.0.0.5"
    assert (await oidc.cache.discovery(ISSUER)).issuer == ISSUER
    for address in ("127.0.0.1", "169.254.169.254"):
        idp.address = address
        oidc.cache.clear()
        with pytest.raises(oidc.IdpError):
            await oidc.cache.discovery(ISSUER)
    assert not any(r.url.host in ("127.0.0.1", "169.254.169.254") for r in idp.requests)


async def test_connections_are_pinned_and_redirects_not_followed(idp):
    def handler(request):
        idp.requests.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})

    oidc.fetcher.transport = httpx.MockTransport(handler)
    with pytest.raises(oidc.IdpError):
        await oidc.cache.discovery(ISSUER)
    assert len(idp.requests) == 1
    req = idp.requests[0]
    assert req.url.host == idp.address  # connected to the checked address
    assert req.headers["host"] == "idp.example.com"


async def test_oversized_documents_are_refused(idp):
    def handler(request):
        return httpx.Response(200, content=b'{"keys": [' + b" " * oidc.MAX_DOCUMENT_BYTES + b"]}")

    oidc.fetcher.transport = httpx.MockTransport(handler)
    with pytest.raises(oidc.IdpError):
        await oidc.fetcher.get_json(JWKS)


def test_https_only():
    for bad in ("http://idp.example.com", "https://user:pw@idp.example.com", "ftp://x", "https://"):
        with pytest.raises(ValueError):
            oidc.https_url(bad)


def test_pkce_challenge_matches_rfc7636_appendix_b():
    assert (
        oidc.pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
