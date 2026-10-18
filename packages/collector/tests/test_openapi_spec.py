"""docs/openapi.yaml against the collector's routes and serializers.

Covers the Partner API routes plus the tenant admin webhook and provider
credential routes the spec documents: every documented operation exists in
the app (and the reverse for those prefixes), and the documented schemas
have exactly the keys the serializers and request bodies use.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import provider_credentials as pc
from collector import provider_credentials_api, webhook_admin, webhooks
from collector.app import app

yaml = pytest.importorskip("yaml")

SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text()
)
SCHEMAS = SPEC["components"]["schemas"]
METHODS = ("get", "post", "put", "patch", "delete")

# Route prefixes the spec documents in full.
DOCUMENTED_PREFIXES = (
    "/partner/v1/",
    "/api/v1/admin/webhooks/",
    "/api/v1/admin/provider-credentials",
    "/api/v1/admin/provider-key-fingerprints",
)


def _props(name: str) -> set[str]:
    schema = SCHEMAS[name]
    out = set(schema.get("properties", {}))
    for part in schema.get("allOf", []):
        ref = part.get("$ref")
        out |= (
            _props(ref.rsplit("/", 1)[-1]) if ref else set(part.get("properties", {}))
        )
    return out


def test_documented_operations_match_the_app():
    spec_ops = {
        (method.upper(), path)
        for path, item in SPEC["paths"].items()
        if path.startswith(DOCUMENTED_PREFIXES)
        for method in METHODS
        if method in item
    }
    # The app's own generated schema lists every mounted route.
    app_ops = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        if path.startswith(DOCUMENTED_PREFIXES)
        for method in METHODS
        if method in item
    }
    assert app_ops, "no routes found"
    assert sorted(spec_ops - app_ops) == [], "documented but not served"
    assert sorted(app_ops - spec_ops) == [], "served but not documented"


def test_webhook_schemas_match_serializers():
    now = datetime.now(UTC)
    sub = SimpleNamespace(
        id=uuid.uuid4(),
        partner_id=uuid.uuid4(),
        url="https://example.com/h",
        event_types=list(webhooks.EVENT_TYPES),
        description=None,
        is_active=True,
        previous_secret_ciphertext=None,
        previous_secret_expires_at=None,
        created_at=now,
        updated_at=now,
    )
    sub_keys = set(webhook_admin._subscription_dict(sub))
    assert _props("WebhookSubscription") == sub_keys
    assert set(SCHEMAS["WebhookSubscription"]["required"]) == sub_keys
    assert _props("WebhookSubscriptionWithSecret") == sub_keys | {"secret", "warning"}

    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        subscription_id=uuid.uuid4(),
        event_id=uuid.uuid4(),
        event_type="asset.created",
        asset_id=None,
        status="dead",
        attempts=8,
        last_status_code=500,
        last_error="HTTP 500",
        next_attempt_at=None,
        last_attempt_at=now,
        delivered_at=None,
        created_at=now,
    )
    delivery_keys = set(webhook_admin._delivery_dict(delivery))
    assert _props("WebhookDelivery") == delivery_keys
    assert set(SCHEMAS["WebhookDelivery"]["required"]) == delivery_keys

    assert SCHEMAS["WebhookEventType"]["enum"] == list(webhooks.EVENT_TYPES)


@pytest.mark.parametrize(
    ("schema", "model"),
    [
        ("CreateWebhookSubscriptionRequest", webhook_admin.CreateSubscriptionBody),
        ("UpdateWebhookSubscriptionRequest", webhook_admin.UpdateSubscriptionBody),
        ("RotateWebhookSecretRequest", webhook_admin.RotateSecretBody),
        ("AdminKeyRequest", provider_credentials_api.AdminKeyBody),
        ("FingerprintRequest", provider_credentials_api.FingerprintBody),
    ],
)
def test_request_bodies_match_models(schema, model):
    assert _props(schema) == set(model.model_fields)
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert set(SCHEMAS[schema].get("required", [])) == required


def test_provider_credential_schemas_match_serializers():
    now = datetime.now(UTC)
    cred = SimpleNamespace(
        provider="openai",
        key_hint="****abcd",
        secret_ciphertext="not-a-token",
        created_at=now,
        rotated_at=None,
    )
    assert _props("ProviderCredential") == set(pc.serialize_credential(cred))
    link = SimpleNamespace(
        provider="openai",
        api_key_id="key_a",
        api_key_fingerprint="0" * 16,
        created_at=now,
    )
    assert _props("ProviderKeyFingerprintLink") == set(pc.serialize_link(link))
    assert SCHEMAS["ProviderCredential"]["properties"]["provider"]["enum"] == list(
        pc.PROVIDERS
    )
    # The admin key is documented as write-only and never in a response.
    assert SCHEMAS["AdminKeyRequest"]["properties"]["admin_key"]["writeOnly"] is True
    assert "admin_key" not in _props("ProviderCredential")


def test_provider_credential_partner_routes_need_the_scope():
    for path, item in SPEC["paths"].items():
        if not path.startswith("/partner/v1/provider-"):
            continue
        for method in METHODS:
            if method in item:
                assert item[method]["security"][0] == {
                    "OAuth2": ["provider-credentials:manage"]
                }, (method, path)
