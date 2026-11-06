"""Stable credential fingerprints for keyless auth (#76).

The literal vectors are also pinned in the gateway suite
(gateway/src/utils/credentialFingerprint.test.ts, "SDK parity"), so the SDK
and the gateway report one fingerprint per identity.
"""
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from prompt_shields import AsyncShieldsClient, ShieldsClient, credentials
from prompt_shields.credentials import (
    anthropic_workload_fingerprint,
    anthropic_workload_fingerprint_from_env,
    aws_key_fingerprint,
    aws_role_fingerprint,
    azure_entra_fingerprint,
    azure_managed_fingerprint,
    azure_workload_fingerprint,
    gcp_service_account_fingerprint,
    key_fingerprint,
    oauth_client_fingerprint,
    openai_workload_fingerprint,
)

VECTORS = [
    (lambda: oauth_client_fingerprint(
        "https://idp.example.com/oauth2/token", "client-a"), "276226128299b028"),
    (lambda: aws_role_fingerprint("arn:aws:iam::123456789012:role/r"), "36d1362714d3ea4e"),
    (lambda: aws_key_fingerprint("AKIASTATIC"), "f6e7cc0275129b17"),
    (lambda: azure_entra_fingerprint("tenant-1", "client-1"), "d90880d979ddcaca"),
    (lambda: azure_managed_fingerprint("mi-client"), "fbcf0f5d67bfb730"),
    (lambda: azure_workload_fingerprint("wi-client"), "082f4758ea8eebff"),
    (lambda: gcp_service_account_fingerprint(
        "sa@proj.iam.gserviceaccount.com"), "721bf1a8e95f0e39"),
    (lambda: openai_workload_fingerprint("svc_acct_456"), "a2488441e9dcd04f"),
    (lambda: anthropic_workload_fingerprint(
        "00000000-0000-0000-0000-000000000001", "svac_def"), "8375b737f5b41df5"),
    (lambda: key_fingerprint("sk-static"), "679bb5c90f55bc80"),
]


@pytest.mark.parametrize(("make", "expected"), VECTORS)
def test_vectors_match_the_gateway(make, expected):
    assert make() == expected


def test_key_fingerprint_is_the_default_client_hash():
    client = ShieldsClient(api_key="sk-static", ps_api_key="ps")
    assert client._api_key_fingerprint == key_fingerprint("sk-static")


def test_identity_kinds_do_not_collide_with_keys():
    # The kind prefix separates an identity from a key with the same text.
    assert aws_key_fingerprint("AKIASTATIC") != key_fingerprint("AKIASTATIC")
    assert azure_managed_fingerprint("x") != azure_workload_fingerprint("x")


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_empty_identity_parts_are_rejected(bad):
    with pytest.raises(ValueError):
        openai_workload_fingerprint(bad)
    with pytest.raises(ValueError):
        oauth_client_fingerprint("https://idp.example.com/t", bad)


def test_anthropic_from_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ORGANIZATION_ID", raising=False)
    monkeypatch.delenv("ANTHROPIC_SERVICE_ACCOUNT_ID", raising=False)
    assert anthropic_workload_fingerprint_from_env() is None
    monkeypatch.setenv("ANTHROPIC_ORGANIZATION_ID", "00000000-0000-0000-0000-000000000001")
    assert anthropic_workload_fingerprint_from_env() is None
    monkeypatch.setenv("ANTHROPIC_SERVICE_ACCOUNT_ID", "svac_def")
    assert anthropic_workload_fingerprint_from_env() == "8375b737f5b41df5"


# --- clients -----------------------------------------------------------------


def _event(client):
    resp = MagicMock()
    resp.model = "gpt-4o"
    resp.usage.prompt_tokens = 1
    resp.usage.completion_tokens = 1
    resp.choices = []
    return client._build_event(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}],
        response=resp, latency_ms=5,
    )


def test_explicit_fingerprint_survives_token_rotation(monkeypatch):
    monkeypatch.delenv("PS_CREDENTIAL_FINGERPRINT", raising=False)
    fp = openai_workload_fingerprint("svc_acct_456")
    # Two short-lived bearer tokens for one workload.
    a = ShieldsClient(api_key="oa-minted-1", ps_api_key="ps", credential_fingerprint=fp)
    b = ShieldsClient(api_key="oa-minted-2", ps_api_key="ps", credential_fingerprint=fp)
    ea, eb = _event(a), _event(b)
    assert ea["api_key_fingerprint"] == eb["api_key_fingerprint"] == fp
    for ev in (ea, eb):
        assert "oa-minted" not in str(ev)


def test_default_is_unchanged_key_hash(monkeypatch):
    monkeypatch.delenv("PS_CREDENTIAL_FINGERPRINT", raising=False)
    a = ShieldsClient(api_key="oa-minted-1", ps_api_key="ps")
    b = ShieldsClient(api_key="oa-minted-2", ps_api_key="ps")
    assert _event(a)["api_key_fingerprint"] != _event(b)["api_key_fingerprint"]


def test_env_fallback(monkeypatch):
    monkeypatch.setenv("PS_CREDENTIAL_FINGERPRINT", "8375b737f5b41df5")
    client = ShieldsClient(api_key="sk-x", ps_api_key="ps")
    assert _event(client)["api_key_fingerprint"] == "8375b737f5b41df5"
    # An explicit argument wins over the environment.
    client = ShieldsClient(api_key="sk-x", ps_api_key="ps", credential_fingerprint="abc")
    assert client._api_key_fingerprint == "abc"


def test_keyless_client_without_fingerprint(monkeypatch):
    # api_key=None lets the vendor SDK use its own credential chain
    # (e.g. OpenAI workload_identity=...), and no key hash is sent.
    monkeypatch.delenv("PS_CREDENTIAL_FINGERPRINT", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    client = ShieldsClient(api_key=None, ps_api_key="ps")
    assert _event(client)["api_key_fingerprint"] is None


def test_async_client_takes_the_same_argument(monkeypatch):
    monkeypatch.delenv("PS_CREDENTIAL_FINGERPRINT", raising=False)
    fp = oauth_client_fingerprint("https://idp.example.com/oauth2/token", "client-a")
    client = AsyncShieldsClient(api_key="tok", ps_api_key="ps", credential_fingerprint=fp)
    assert client._api_key_fingerprint == fp


def test_instrumentation_build_event_uses_state_fingerprint():
    from prompt_shields.instrumentation import build_event

    state = NS(metadata={}, scan_pii=False, send_prompt_text=False,
               pricing_table=None, credential_fingerprint="36d1362714d3ea4e")
    event = build_event(state, vendor="openai", model="gpt-4o", messages=[],
                        latency_ms=1, adapter_fields={}, api_key="oa-minted-9")
    assert event["api_key_fingerprint"] == "36d1362714d3ea4e"
    state.credential_fingerprint = None
    event = build_event(state, vendor="openai", model="gpt-4o", messages=[],
                        latency_ms=1, adapter_fields={}, api_key="sk-static")
    assert event["api_key_fingerprint"] == key_fingerprint("sk-static")


def test_module_exports():
    assert set(credentials.__all__) >= {"openai_workload_fingerprint", "key_fingerprint"}
