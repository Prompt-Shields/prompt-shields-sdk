"""Unit tests for partner change webhooks: signing, secret encryption and
destination (SSRF) checks. No database needed."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import pathlib
import re
import shutil
import subprocess
import time
import uuid

import httpx
import pytest

from collector import webhooks
from collector.config import settings

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", KEY)
    monkeypatch.setattr(settings, "webhook_secret_keys", "")
    monkeypatch.setattr(settings, "webhook_allow_http", False)
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", False)


def _resolver(*addresses):
    async def resolve(host, port):
        return list(addresses)

    return resolve


# ── signatures ────────────────────────────────────────────────────────


def test_signature_round_trip():
    body = b'{"id":"e1"}'
    ts = int(time.time())
    header = webhooks.sign_payload("whsec_x", body, ts)
    assert header.startswith(f"t={ts},v1=")
    assert webhooks.verify_signature("whsec_x", body, header)


@pytest.mark.parametrize(
    "secret, body, header_ts_offset",
    [
        ("whsec_other", b'{"id":"e1"}', 0),  # wrong secret
        ("whsec_x", b'{"id":"e2"}', 0),  # tampered body
        ("whsec_x", b'{"id":"e1"}', -301),  # replay: too old
    ],
)
def test_signature_rejects(secret, body, header_ts_offset):
    ts = int(time.time()) + header_ts_offset
    header = webhooks.sign_payload("whsec_x", b'{"id":"e1"}', ts)
    assert not webhooks.verify_signature(secret, body, header)


@pytest.mark.parametrize("header", ["", "garbage", "t=abc,v1=00", "v1=00"])
def test_signature_rejects_malformed_header(header):
    assert not webhooks.verify_signature("whsec_x", b"{}", header)


# ── secret encryption ─────────────────────────────────────────────────


def test_secret_encryption_round_trip_and_binding():
    sid = uuid.uuid4()
    secret = webhooks.generate_signing_secret()
    token = webhooks.encrypt_secret(secret, sid)
    assert secret not in token and token.startswith("v1.")
    assert webhooks.decrypt_secret(token, sid) == secret
    # Bound to the subscription id: a ciphertext copied to another row fails.
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token, uuid.uuid4())


def test_secret_unreadable_after_key_change(monkeypatch):
    sid = uuid.uuid4()
    token = webhooks.encrypt_secret("whsec_x", sid)
    monkeypatch.setattr(settings, "webhook_secret_key", "z" * 40)
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token, sid)


@pytest.mark.parametrize("key", ["", "short"])
def test_missing_or_short_key_is_not_configured(monkeypatch, key):
    monkeypatch.setattr(settings, "webhook_secret_key", key)
    with pytest.raises(webhooks.WebhooksNotConfigured):
        webhooks.encrypt_secret("whsec_x", uuid.uuid4())


# ── destination checks ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https:///nohost",
        "https://hooks.example.com:99999/x",
        "https://hooks.example.com/" + "a" * 3000,
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(webhooks.DestinationNotAllowed):
        webhooks.check_url(url)


def test_http_allowed_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_http", True)
    assert webhooks.check_url("http://hooks.example.com/x").hostname == "hooks.example.com"


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:a00:1::",  # 6to4 wrapping 10.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "::a9fe:a9fe",  # IPv4-compatible (deprecated)
        "::8.8.8.8",  # IPv4-compatible, even if public
        "fec0::1",  # site-local (deprecated)
        "fd00:ec2::254",  # AWS IPv6 metadata
        "198.18.0.1",  # benchmarking
    ],
)
def test_non_public_addresses_blocked(addr):
    assert not webhooks.ip_allowed(ipaddress.ip_address(addr))


@pytest.mark.parametrize(
    "addr", ["93.184.216.34", "2606:4700::1111", "64:ff9b::808:808", "::ffff:8.8.8.8"]
)
def test_public_addresses_allowed(addr):
    assert webhooks.ip_allowed(ipaddress.ip_address(addr))


def test_private_allowed_only_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_allow_private_destinations", True)
    assert webhooks.ip_allowed(ipaddress.ip_address("10.0.0.1"))
    assert not webhooks.ip_allowed(ipaddress.ip_address("0.0.0.0"))


async def test_resolve_refuses_any_private_record(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("93.184.216.34", "10.0.0.5"))
    with pytest.raises(webhooks.DestinationNotAllowed):
        await webhooks.resolve_destination("https://hooks.example.com/x")


async def test_resolve_failure_is_refused_with_same_message(monkeypatch):
    async def fail(host, port):
        raise OSError("nxdomain")

    monkeypatch.setattr(webhooks, "_resolve_host", fail)
    with pytest.raises(webhooks.DestinationNotAllowed, match="public addresses"):
        await webhooks.resolve_destination("https://nope.example.com/x")


async def test_post_signed_pins_checked_address_and_does_not_follow_redirects(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("93.184.216.34"))
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    status = await webhooks.post_signed(
        "https://hooks.example.com:8443/in?x=1",
        b"{}",
        {"X-Test": "1"},
        transport=httpx.MockTransport(handler),
    )
    assert status == 302
    assert len(seen) == 1  # redirect not followed
    req = seen[0]
    assert req.url.host == "93.184.216.34" and req.url.port == 8443
    assert req.url.path == "/in" and req.url.query == b"x=1"
    assert req.headers["Host"] == "hooks.example.com:8443"
    assert req.extensions["sni_hostname"] == "hooks.example.com"


async def test_post_signed_ipv6_target(monkeypatch):
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("2606:4700::1111"))
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    await webhooks.post_signed(
        "https://hooks.example.com/", b"{}", {}, transport=httpx.MockTransport(handler)
    )
    assert seen[0].url.host == "2606:4700::1111"


def test_retry_schedule_is_bounded():
    assert webhooks.retry_delay(1).total_seconds() == 60
    last = webhooks.RETRY_SCHEDULE_SECONDS[-1]
    assert webhooks.retry_delay(50).total_seconds() == last


def test_payload_excludes_owner_email():
    from db.models import AIAsset

    asset = AIAsset(
        id=uuid.uuid4(), vendor="openai", owner_email="a@b.c", discovery_source=["sdk"]
    )
    payload = webhooks.asset_payload(asset)
    assert "owner_email" not in payload
    assert payload["vendor"] == "openai"
    assert payload["modality"] is None
    asset.modality = "document"
    assert webhooks.asset_payload(asset)["modality"] == "document"


async def test_post_signed_has_an_overall_deadline(monkeypatch):
    """httpx timeouts are per operation; a trickling server must not hold
    an attempt past PS_WEBHOOK_TIMEOUT_SECONDS (and so past the lease)."""
    monkeypatch.setattr(webhooks, "_resolve_host", _resolver("93.184.216.34"))
    monkeypatch.setattr(settings, "webhook_timeout_seconds", 0.2)

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(5)
            return httpx.Response(200)

    with pytest.raises(httpx.TimeoutException):
        await webhooks.post_signed("https://hooks.example.com/", b"{}", {}, transport=Slow())


# ── encryption keyring ────────────────────────────────────────────────

K1 = "1" * 40
K2 = "2" * 40


def test_legacy_key_alone_keeps_the_v1_format():
    sid = uuid.uuid4()
    token = webhooks.encrypt_secret("whsec_x", sid)
    assert token.startswith("v1.") and webhooks.token_key_id(token) is None
    assert not webhooks.needs_rekey(token)


def test_keyring_encrypts_with_the_first_key_and_records_its_id(monkeypatch):
    sid = uuid.uuid4()
    legacy = webhooks.encrypt_secret("whsec_old", sid)
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2}, k1:{K1}")
    token = webhooks.encrypt_secret("whsec_x", sid)
    assert token.startswith("v2.k2.")
    assert webhooks.decrypt_secret(token, sid) == "whsec_x"
    # Old ciphertexts without a key id still decrypt with the legacy key.
    assert webhooks.decrypt_secret(legacy, sid) == "whsec_old"
    assert webhooks.needs_rekey(legacy) and not webhooks.needs_rekey(token)

    # k1 becomes active: k2 tokens still decrypt, and need a rekey.
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1},k2:{K2}")
    assert webhooks.decrypt_secret(token, sid) == "whsec_x"
    assert webhooks.needs_rekey(token)
    assert webhooks.encrypt_secret("whsec_x", sid).startswith("v2.k1.")

    # Without the legacy key, legacy tokens are unreadable (not a crash).
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(legacy, sid)
    # A removed key makes its tokens unreadable.
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    with pytest.raises(ValueError, match="k2"):
        webhooks.decrypt_secret(token, sid)


def test_keyring_token_is_bound_to_key_id_and_subscription(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1},k2:{K1}")
    sid = uuid.uuid4()
    token = webhooks.encrypt_secret("whsec_x", sid)
    # Relabelled to another key id with the same key material: rejected.
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token.replace("v2.k1.", "v2.k2.", 1), sid)
    with pytest.raises(ValueError):
        webhooks.decrypt_secret(token, uuid.uuid4())


@pytest.mark.parametrize(
    "keys",
    [
        "nocolon" + "x" * 40,
        f"bad id:{K1}",
        f"k1:{K1},k1:{K2}",
        "k1:short",
        f"k1:{K1},",
    ],
)
def test_malformed_keyring_is_not_configured_and_never_echoes_keys(monkeypatch, keys):
    monkeypatch.setattr(settings, "webhook_secret_keys", keys)
    with pytest.raises(webhooks.WebhooksNotConfigured) as exc:
        webhooks.encrypt_secret("whsec_x", uuid.uuid4())
    assert K1 not in str(exc.value) and K2 not in str(exc.value)


def test_no_key_at_all_is_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    with pytest.raises(webhooks.WebhooksNotConfigured):
        webhooks.active_key_id()


# ── secret rotation: several signatures ───────────────────────────────


def test_header_carries_one_v1_per_secret_and_either_verifies():
    body = b'{"id":"e1"}'
    ts = int(time.time())
    header = webhooks.sign_payload(["whsec_new", "whsec_old"], body, ts)
    assert header.count("v1=") == 2 and header.startswith(f"t={ts},v1=")
    assert webhooks.verify_signature("whsec_new", body, header)
    assert webhooks.verify_signature("whsec_old", body, header)
    assert not webhooks.verify_signature("whsec_other", body, header)
    # A single secret gives the original one-signature header.
    assert webhooks.sign_payload("whsec_new", body, ts).count("v1=") == 1


# ── attempt deadline bound ────────────────────────────────────────────


@pytest.mark.parametrize("value", [0, -1, 61, 300, 600])
def test_timeout_setting_must_fit_inside_the_claim_lease(value):
    from collector.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="PS_WEBHOOK_TIMEOUT_SECONDS"):
        Settings(webhook_timeout_seconds=value)


def test_timeout_setting_accepts_values_up_to_the_cap(monkeypatch):
    from collector.config import WEBHOOK_MAX_TIMEOUT_SECONDS, Settings

    assert Settings(webhook_timeout_seconds=60).webhook_timeout_seconds == 60
    # A value set at runtime (bypassing validation) is still capped.
    monkeypatch.setattr(settings, "webhook_timeout_seconds", 900.0)
    assert webhooks.attempt_timeout_seconds() == WEBHOOK_MAX_TIMEOUT_SECONDS
    assert WEBHOOK_MAX_TIMEOUT_SECONDS < webhooks.CLAIM_LEASE.total_seconds()


# ── missing keys are told apart from unreadable secrets ───────────────


def test_missing_key_is_secret_key_missing_but_bad_tokens_are_not(monkeypatch):
    sid = uuid.uuid4()
    legacy = webhooks.encrypt_secret("whsec_old", sid)
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2}")
    token = webhooks.encrypt_secret("whsec_x", sid)

    monkeypatch.setattr(settings, "webhook_secret_keys", f"k1:{K1}")
    with pytest.raises(webhooks.SecretKeyMissing) as exc:
        webhooks.decrypt_secret(token, sid)
    assert exc.value.key_id == "k2" and K2 not in str(exc.value)
    monkeypatch.setattr(settings, "webhook_secret_key", "")
    with pytest.raises(webhooks.SecretKeyMissing) as exc:
        webhooks.decrypt_secret(legacy, sid)
    assert exc.value.key_id is None

    # Configured key, bad token: an ordinary ValueError (dead-lettered).
    monkeypatch.setattr(settings, "webhook_secret_keys", f"k2:{K2}")
    for bad in (token[:-4] + "AAAA", "v2.k2.!!!", "v2.k2.AAAA"):
        with pytest.raises(ValueError) as exc:
            webhooks.decrypt_secret(bad, sid)
        assert not isinstance(exc.value, webhooks.SecretKeyMissing)


# ── malformed signature headers never raise ───────────────────────────

BODY = b'{"id":"e1"}'
DOCS_PAGE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs"
    / "api-reference"
    / "sync"
    / "webhooks.mdx"
)


def _signature_cases() -> list[tuple[str, object]]:
    """(name, header) pairs: valid, rotated and malformed headers."""
    ts = int(time.time())
    good = webhooks.sign_payload("whsec_x", BODY, ts)
    v1 = good.split("v1=", 1)[1]
    return [
        ("valid", good),
        ("rotated", webhooks.sign_payload(["whsec_new", "whsec_x"], BODY, ts)),
        ("valid with junk item", f"{good},junk"),
        ("junk item first", f"junk,{good}"),
        ("junk item and bad v1", "t=1,v1=x,junk"),
        ("current t, bad v1, junk", f"t={ts},v1=x,junk"),
        ("non-ASCII v1", f"t={ts},v1=é{v1[1:]}"),
        ("non-ASCII v1 only", f"t={ts},v1=ü"),
        ("non-ASCII digits in t", f"t=١٢٣,v1={v1}"),
        ("superscript t", f"t=²,v1={v1}"),
        ("huge t", "t=" + "9" * 500 + f",v1={v1}"),
        ("signed t", f"t=+{ts},v1={v1}"),
        ("float t", f"t={ts}.0,v1={v1}"),
        ("spaces", f"t= {ts},v1={v1}"),
        ("no v1", f"t={ts}"),
        ("no t", f"v1={v1}"),
        ("empty", ""),
        ("only commas", ",,,"),
        ("equals only", "="),
        ("emoji", "t=🙂,v1=🙂"),
        ("lone surrogate", f"t={ts},v1=\udcff"),
        ("not a string", None),
        ("bytes", good.encode()),
    ]


EXPECTED_VALID = {"valid", "rotated", "valid with junk item", "junk item first"}


@pytest.mark.parametrize("name, header", _signature_cases())
def test_verify_signature_never_raises(name, header):
    assert webhooks.verify_signature("whsec_x", BODY, header) is (name in EXPECTED_VALID)


def _docs_block(language: str, marker: str) -> str:
    """The fenced `language` block in the webhooks docs that contains `marker`."""
    blocks = re.findall(rf"```{language}\n(.*?)```", DOCS_PAGE.read_text(), re.DOTALL)
    (block,) = [b for b in blocks if marker in b]
    return block


def test_documented_python_sample_matches_the_reference():
    namespace: dict = {}
    source = _docs_block("python", "def verify_prompt_shields_signature")
    exec(compile(source, "webhooks.mdx", "exec"), namespace)  # noqa: S102
    verify = namespace["verify_prompt_shields_signature"]
    for name, header in _signature_cases():
        assert verify("whsec_x", BODY, header) is (name in EXPECTED_VALID), name
    stale = webhooks.sign_payload("whsec_x", BODY, int(time.time()) - 301)
    assert verify("whsec_x", BODY, stale) is False


def test_documented_node_sample_matches_the_reference(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    (tmp_path / "verify.mjs").write_text(_docs_block("javascript", "verifyPromptShieldsSignature"))
    (tmp_path / "run.mjs").write_text(
        'import { verifyPromptShieldsSignature as verify } from "./verify.mjs";\n'
        'import { readFileSync } from "node:fs";\n'
        'const cases = JSON.parse(readFileSync(0, "utf8"));\n'
        "const body = Buffer.from(cases.body);\n"
        "const out = {};\n"
        "for (const [name, header] of cases.headers) {\n"
        "  try { out[name] = verify('whsec_x', body, header); }\n"
        "  catch (e) { out[name] = 'threw: ' + e; }\n"
        "}\n"
        "out['array header'] = verify('whsec_x', body, ['t=1', 'v1=x']);\n"
        "console.log(JSON.stringify(out));\n"
    )
    stale = webhooks.sign_payload("whsec_x", BODY, int(time.time()) - 301)
    headers = [
        (name, header if isinstance(header, str) else None)
        for name, header in _signature_cases()
        if name not in ("lone surrogate", "bytes")  # not representable in JSON
    ] + [("stale", stale)]
    result = subprocess.run(
        [node, str(tmp_path / "run.mjs")],
        input=json.dumps({"body": BODY.decode(), "headers": headers}),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    expected = {name: name in EXPECTED_VALID for name, _ in headers}
    expected["array header"] = False
    assert json.loads(result.stdout) == expected
