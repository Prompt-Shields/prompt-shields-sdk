"""docs/openapi.yaml against the collector's routes and serializers.

Covers the Partner API, every tenant admin route (/api/v1/admin) and every
operator route (/api/v1/operator): every documented operation exists in
the app (and the reverse for those prefixes), each admin operation's
security and ``x-admin-scope`` match the route's dependency, and the
documented schemas have exactly the keys the serializers and request
bodies use.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import (
    admin_api,
    cloud_inventory,
    leanix,
    lifecycle,
    operator_admin,
    partner_admin,
    provider_credentials_api,
    provider_usage_admin,
    risk_reviews,
    risk_suggestions,
    servicenow,
    tenant_keys,
    webhook_admin,
    webhooks,
)
from collector import provider_credentials as pc
from collector.admin_auth import ADMIN_SCOPES, serialize_token
from collector.app import app
from fastapi.routing import APIRoute

yaml = pytest.importorskip("yaml")

SPEC = yaml.safe_load(
    (Path(__file__).resolve().parents[3] / "docs" / "openapi.yaml").read_text()
)
SCHEMAS = SPEC["components"]["schemas"]
METHODS = ("get", "post", "put", "patch", "delete")

# Route prefixes the spec documents in full.
DOCUMENTED_PREFIXES = (
    "/partner/v1/",
    "/api/v1/admin/",
    "/api/v1/operator/",
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

    assert SCHEMAS["WebhookEventType"]["enum"] == list(webhooks.SUBSCRIBABLE_EVENT_TYPES)


@pytest.mark.parametrize(
    ("schema", "model"),
    [
        ("CreateWebhookSubscriptionRequest", webhook_admin.CreateSubscriptionBody),
        ("UpdateWebhookSubscriptionRequest", webhook_admin.UpdateSubscriptionBody),
        ("RotateWebhookSecretRequest", webhook_admin.RotateSecretBody),
        ("AdminKeyRequest", provider_credentials_api.AdminKeyBody),
        ("FingerprintRequest", provider_credentials_api.FingerprintBody),
        ("CreateAdminTokenRequest", admin_api.CreateAdminTokenBody),
        ("CreateTenantApiKeyRequest", admin_api.CreateApiKeyBody),
        ("RotateTenantApiKeyRequest", admin_api.RotateApiKeyBody),
        ("IssuePartnerCredentialRequest", partner_admin.IssuePartnerCredentialBody),
        ("UpdatePartnerCredentialRequest", partner_admin.UpdatePartnerCredentialBody),
        ("CreateTenantRequest", operator_admin.CreateTenantBody),
        ("UpdateTenantRequest", operator_admin.UpdateTenantBody),
        ("BootstrapAdminTokenRequest", operator_admin.BootstrapAdminTokenBody),
        ("RiskReviewRequest", risk_reviews.ReviewBody),
    ],
)
def test_request_bodies_match_models(schema, model):
    assert _props(schema) == set(model.model_fields)
    required = {n for n, f in model.model_fields.items() if f.is_required()}
    assert set(SCHEMAS[schema].get("required", [])) == required


def test_risk_review_schemas_match_serializers():
    now = datetime.now(UTC)
    mapping = SimpleNamespace(
        id=uuid.uuid4(),
        risk_category="employment",
        risk_level="high",
        framework="EU_AI_ACT",
        status="confirmed",
        source="rules",
        rule_id="r",
        ruleset_version="1",
        framework_ref="Annex III",
        confidence="high",
        evidence=None,
        reviewed_at=now,
        reviewed_by="dpo@example.com",
        review_comment="ok",
    )
    assert _props("RiskMapping") == set(risk_suggestions.risk_dict(mapping))
    review = SimpleNamespace(
        id=uuid.uuid4(),
        risk_mapping_id=uuid.uuid4(),
        asset_id=uuid.uuid4(),
        status="rejected",
        previous_status="suggested",
        comment="not in scope",
        reviewed_by="dpo@example.com",
        created_at=now,
        actor_type="partner",
        partner_id=uuid.uuid4(),
        snapshot={},
    )
    keys = set(risk_reviews.serialize_review(review))
    assert _props("RiskMappingReview") == keys
    assert set(SCHEMAS["RiskMappingReview"]["required"]) == keys


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


# ──────────────────────────────────────────────────────────────────────
# Tenant admin and operator routes
# ──────────────────────────────────────────────────────────────────────


# Every router mounted under /api/v1/admin or /api/v1/operator. A router
# missing here fails the test below with a KeyError on its paths.
ADMIN_ROUTERS = (
    admin_api.router,
    partner_admin.router,
    webhook_admin.router,
    provider_credentials_api.tenant_router,
    lifecycle.router,
    risk_suggestions.router,
    risk_reviews.admin_router,
    provider_usage_admin.router,
    cloud_inventory.router,
    leanix.router,
    servicenow.router,
    operator_admin.router,
)


def _route_admin_scopes() -> dict[tuple[str, str], str | None]:
    """(METHOD, path) -> the admin scope its require_admin dependency
    declares, or None for a route without one (operator routes)."""
    out = {}
    for route in (r for router in ADMIN_ROUTERS for r in router.routes):
        if not isinstance(route, APIRoute):
            continue
        scopes = {
            getattr(dep.call, "admin_scope", None) for dep in route.dependant.dependencies
        } - {None}
        assert len(scopes) <= 1, route.path
        for method in route.methods:
            out[(method, route.path)] = next(iter(scopes), None)
    return out


def test_admin_operations_document_their_scope_and_security():
    scopes = _route_admin_scopes()
    assert scopes
    for path, item in SPEC["paths"].items():
        if not path.startswith(("/api/v1/admin/", "/api/v1/operator/")):
            continue
        for method in METHODS:
            if method not in item:
                continue
            op = item[method]
            scope = scopes[(method.upper(), path)]
            if scope is None:
                # Operator routes take PS_ADMIN_TOKEN, never a tenant credential.
                assert op["security"] == [{"OperatorToken": []}], (method, path)
                assert "x-admin-scope" not in op, (method, path)
            else:
                assert op["security"] == [{"TenantApiKey": []}, {"AdminToken": []}], (
                    method,
                    path,
                )
                assert op["x-admin-scope"] == scope, (method, path)
                assert op["responses"]["403"] == {
                    "$ref": "#/components/responses/AdminForbidden"
                }, (method, path)


def test_admin_scope_enum_matches_code():
    assert SCHEMAS["AdminScope"]["enum"] == list(ADMIN_SCOPES)


def test_admin_credential_schemas_match_serializers():
    now = datetime.now(UTC)
    token = SimpleNamespace(
        id=uuid.uuid4(),
        name="terraform",
        scopes=["admin:read"],
        token_hint="abcd",
        created_by="operator",
        created_at=now,
        expires_at=None,
        revoked_at=None,
    )
    keys = set(serialize_token(token))
    assert _props("AdminToken") == keys == set(SCHEMAS["AdminToken"]["required"])
    assert _props("AdminTokenWithSecret") == keys | {"token", "warning"}

    key = SimpleNamespace(
        id=uuid.uuid4(),
        name="gateway",
        key_hint="abcd",
        origin="api",
        previous_key_hash=None,
        previous_expires_at=None,
        created_at=now,
        rotated_at=None,
        expires_at=None,
        revoked_at=None,
    )
    keys = set(tenant_keys.serialize_key(key))
    assert _props("TenantApiKey") == keys == set(SCHEMAS["TenantApiKey"]["required"])
    assert _props("TenantApiKeyWithSecret") == keys | {"key", "warning"}
    assert SCHEMAS["CredentialStatus"]["enum"] == list(tenant_keys.STATUSES)


def test_partner_credential_schemas_match_serializers():
    cred = SimpleNamespace(
        id=uuid.uuid4(),
        partner_name="ardoq",
        client_id="ps-client-ardoq",
        scopes=["assets:read"],
        rate_limit=None,
        rate_limit_tier="standard",
        is_active="true",
        api_key_hash=None,
        expires_at=None,
        created_at=datetime.now(UTC),
    )
    keys = set(partner_admin._credential_dict(cred))
    assert _props("PartnerCredential") == keys == set(SCHEMAS["PartnerCredential"]["required"])
    issued = set(partner_admin.IssuePartnerCredentialResponse.model_fields)
    assert _props("PartnerCredentialWithSecrets") == issued


def test_tenant_schema_matches_serializer():
    tenant = SimpleNamespace(
        id=uuid.uuid4(),
        name="Nordbank",
        domain=None,
        settings={},
        disabled_at=None,
        created_at=datetime.now(UTC),
    )
    keys = set(operator_admin._tenant_dict(tenant))
    assert _props("Tenant") == keys == set(SCHEMAS["Tenant"]["required"])
    assert _props("TenantCreated") == keys | {"api_key", "warning"}
