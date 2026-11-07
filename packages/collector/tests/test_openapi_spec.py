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
    audit_trail,
    cloud_inventory,
    cloud_inventory_import,
    leanix,
    learner,
    lifecycle,
    mcp_usage,
    operator_admin,
    operator_auth,
    partner_admin,
    policies_api,
    provider_credentials_api,
    provider_usage_admin,
    rbac,
    rbac_admin,
    risk_reviews,
    risk_suggestions,
    security_events,
    sentinel_export,
    servicenow,
    siem_export,
    tenant_keys,
    user_erasure,
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
        ("CreateOperatorTokenRequest", operator_admin.CreateOperatorTokenBody),
        ("RiskReviewRequest", risk_reviews.ReviewBody),
        ("LearnerReviewRequest", learner.LearnerReviewBody),
        ("EraseUserRequest", user_erasure.EraseUserBody),
        ("CreatePrincipalRequest", rbac_admin.CreatePrincipalBody),
        ("UpdatePrincipalRequest", rbac_admin.UpdatePrincipalBody),
        ("CreateTeamRequest", rbac_admin.CreateTeamBody),
        ("UpdateTeamRequest", rbac_admin.UpdateTeamBody),
        ("CreateProjectRequest", rbac_admin.CreateProjectBody),
        ("UpdateProjectRequest", rbac_admin.UpdateProjectBody),
        ("CreateMembershipRequest", rbac_admin.CreateMembershipBody),
        ("UpdateMembershipRequest", rbac_admin.UpdateMembershipBody),
        ("CreatePolicyRequest", policies_api.CreatePolicyBody),
        ("UpdatePolicyRequest", policies_api.UpdatePolicyBody),
        ("CreatePolicyVersionRequest", policies_api.CreateVersionBody),
        ("ActivatePolicyRequest", policies_api.ActivateBody),
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


def test_learner_schemas_match_serializers():
    now = datetime.now(UTC)
    snap = SimpleNamespace(
        id=uuid.uuid4(),
        learner_version="lv1-0123456789abcdef",
        parent_version=None,
        mode="recommend",
        algorithm="linear-thompson.v1",
        feature_schema="ps-prompt-features.v1",
        policy_version=None,
        data_fingerprint="0" * 64,
        training_rows=1,
        scored_rows=1,
        snapshot={"recommendations": []},
        created_at=now,
    )
    summary = set(learner.serialize_snapshot(snap))
    assert _props("LearnerSnapshotSummary") == summary
    assert set(SCHEMAS["LearnerSnapshotSummary"]["required"]) == summary
    assert _props("LearnerSnapshot") == set(learner.serialize_snapshot(snap, full=True))
    rec = SimpleNamespace(
        id=uuid.uuid4(),
        snapshot_id=uuid.uuid4(),
        learner_version="lv1-0123456789abcdef",
        use_case="support",
        sensitivity_level="internal",
        current_model="openai:gpt-4o",
        recommended_model="mistral:mistral-small",
        candidates=["mistral:mistral-small"],
        projection={},
        status="accepted",
        reviewed_by="a@example.com",
        reviewed_at=now,
        review_comment=None,
        reviewer_actor_type="partner",
        created_at=now,
    )
    keys = set(learner.serialize_recommendation(rec))
    assert _props("LearnerRecommendation") == keys
    assert set(SCHEMAS["LearnerRecommendation"]["required"]) == keys
    realised_keys = set(SCHEMAS["LearnerRealised"]["required"])
    assert _props("LearnerRealised") == realised_keys


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


def test_mcp_schemas_match_serializers():
    now = datetime.now(UTC)
    asset = {"x": ("id", "owner")}
    usage = SimpleNamespace(
        server_name="x", tool_name="t", source="gateway", calling_service="",
        environment="", transport=None, call_count=1, error_count=0,
        blocked_count=0, user_hash="", user_group="", latency_ms_total=0,
        latency_count=0, first_seen=now, last_seen=now,
    )
    assert _props("McpTool") == set(mcp_usage._with_asset(mcp_usage._usage_dict(usage), asset))
    daily = SimpleNamespace(
        day=now.date(), source="gateway", server_name="x", tool_name="t",
        call_count=1, error_count=0, blocked_count=0, latency_ms_total=0,
        latency_count=0, request_bytes=0, response_bytes=0, bytes_count=0,
    )
    assert _props("McpUsageDay") == set(mcp_usage._with_asset(mcp_usage._daily_dict(daily), asset))
    # The serializers always emit every key (null when unknown), so the
    # spec lists them all as required; the TypeScript SDK relies on it.
    for name in ("McpTool", "McpUsageDay"):
        assert set(SCHEMAS[name]["required"]) == _props(name), name
    for name in ("McpToolPage", "McpUsagePage"):
        assert set(SCHEMAS[name]["required"]) == {"data", "meta"}, name
    assert set(SCHEMAS["McpUsagePage"]["properties"]["meta"]["allOf"][1]["required"]) == {"since", "until"}
    for path in ("/partner/v1/mcp/tools", "/partner/v1/mcp/usage"):
        assert SPEC["paths"][path]["get"]["security"][0] == {"OAuth2": ["mcp:read"]}
    assert "/api/v1/registry/mcp-tools" in SPEC["paths"]


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
    cloud_inventory_import.router,
    leanix.router,
    servicenow.router,
    sentinel_export.router,
    rbac_admin.router,
    siem_export.router,
    operator_admin.router,
    user_erasure.router,
    policies_api.router,
)


def _route_scopes(attr: str) -> dict[tuple[str, str], str | None]:
    """(METHOD, path) -> the scope its dependency declares in ``attr``
    (``admin_scope``: require_admin; ``operator_scope``: an operator route,
    collector.operator_auth), or None for a route without one."""
    out = {}
    for route in (r for router in ADMIN_ROUTERS for r in router.routes):
        if not isinstance(route, APIRoute):
            continue
        scopes = {
            getattr(dep.call, attr, None) for dep in route.dependant.dependencies
        } - {None}
        assert len(scopes) <= 1, route.path
        for method in route.methods:
            out[(method, route.path)] = next(iter(scopes), None)
    return out


def _route_admin_scopes() -> dict[tuple[str, str], str | None]:
    return _route_scopes("admin_scope")


def test_admin_operations_document_their_scope_and_security():
    scopes = _route_admin_scopes()
    operator_scopes = _route_scopes("operator_scope")
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
                # Operator routes take an operator token (named, or
                # PS_ADMIN_TOKEN as break-glass), never a tenant credential.
                assert op["security"] == [{"OperatorToken": []}], (method, path)
                assert "x-admin-scope" not in op, (method, path)
                operator_scope = operator_scopes[(method.upper(), path)]
                assert operator_scope, (method, path)
                assert op["x-operator-scope"] == operator_scope, (method, path)
                assert op["responses"]["403"] == {
                    "$ref": "#/components/responses/OperatorForbidden"
                }, (method, path)
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


def test_operator_and_ingest_key_scope_enums_match_code():
    assert SCHEMAS["OperatorScope"]["enum"] == list(operator_auth.OPERATOR_SCOPES)
    assert SCHEMAS["TenantApiKeyScope"]["enum"] == list(tenant_keys.KEY_SCOPES)


def test_operator_token_schema_matches_serializer():
    now = datetime.now(UTC)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        name="ci-deploy",
        scopes=["operator:read"],
        token_hint="abcd",
        created_by="operator",
        created_at=now,
        rotated_at=None,
        expires_at=None,
        revoked_at=None,
    )
    keys = set(operator_auth.serialize_token(row))
    assert (
        _props("OperatorTokenRecord")
        == keys
        == set(SCHEMAS["OperatorTokenRecord"]["required"])
    )
    assert _props("OperatorTokenWithSecret") == keys | {"token", "warning"}


def test_admin_credential_schemas_match_serializers():
    now = datetime.now(UTC)
    token = SimpleNamespace(
        id=uuid.uuid4(),
        name="terraform",
        scopes=["admin:read"],
        token_hint="abcd",
        principal_id=None,
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
        scopes=["ingest:write"],
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
        principal_id=uuid.uuid4(),
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


def test_focus_cost_row_schema_matches_the_export_columns():
    from collector import focus_export

    assert list(SCHEMAS["FocusCostRow"]["properties"]) == list(focus_export.COLUMNS)
    assert SCHEMAS["FocusCostRow"]["required"] == list(focus_export.COLUMNS)
    op = SPEC["paths"]["/partner/v1/usage/focus-export"]["get"]
    assert op["security"][0] == {"OAuth2": ["usage:export"]}
    params = {p["name"]: p["schema"] for p in op["parameters"]}
    assert params["format"]["enum"] == ["csv", "jsonl"]
    assert params["granularity"]["enum"] == ["hour", "day"]
    assert params["cost_mode"]["enum"] == ["estimated", "merge_safe"]
    assert set(focus_export.MEDIA_TYPES) == set(params["format"]["enum"])


def test_usage_series_schemas_match_the_serializer():
    from typing import get_args

    from collector import usage_rollups

    row = {c: 0 for c in usage_rollups.SUM_COLUMNS} | {
        "bucket_start": datetime(2026, 10, 1, tzinfo=UTC),
        "key": None,
    }
    point = usage_rollups.series_point(row)
    assert list(point) == list(usage_rollups.SERIES_COLUMNS)
    assert list(SCHEMAS["UsageSeriesPoint"]["properties"]) == list(point)
    assert SCHEMAS["UsageSeriesPoint"]["required"] == list(point)
    meta = SCHEMAS["UsageSeries"]["properties"]["meta"]
    assert set(meta["properties"]) == set(meta["required"])
    assert meta["properties"]["group_by"]["enum"] == list(get_args(usage_rollups.GroupBy))
    assert meta["properties"]["granularity"]["enum"] == list(
        get_args(usage_rollups.Granularity)
    )
    for path, scope in (
        ("/partner/v1/usage/series", "usage:read"),
        ("/partner/v1/usage/series/export", "usage:export"),
    ):
        op = SPEC["paths"][path]["get"]
        assert op["security"][0] == {"OAuth2": [scope]}
        params = {p["name"]: p["schema"] for p in op["parameters"]}
        assert params["group_by"]["enum"] == list(get_args(usage_rollups.GroupBy))
        assert params["granularity"]["enum"] == list(get_args(usage_rollups.Granularity))
    export = SPEC["paths"]["/partner/v1/usage/series/export"]["get"]
    fmt = {p["name"]: p["schema"] for p in export["parameters"]}["format"]
    assert fmt["enum"] == list(get_args(usage_rollups.ExportFormat))


async def test_usage_series_meta_matches_the_schema(monkeypatch):
    from collector import usage_rollups

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return []

    class _Db:
        async def execute(self, *_a, **_k):
            return _Rows()

    async def no_mark(db, tenant_id):
        return None

    monkeypatch.setattr(usage_rollups, "watermark", no_mark)
    window = usage_rollups.Window(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC))
    body = await usage_rollups.series_page(
        _Db(), uuid.uuid4(), group_by="none", granularity="day", window=window,
        limit=10, offset=0,
    )
    meta = SCHEMAS["UsageSeries"]["properties"]["meta"]
    assert set(body["meta"]) == set(meta["properties"])
    assert set(body) == set(SCHEMAS["UsageSeries"]["required"])


def test_user_erasure_and_audit_schemas_match_serializers():
    result = user_erasure.UserEraseResult(user_id_hash=None, input="user_id")
    keys = set(result.as_dict())
    assert _props("UserErasureResult") == keys
    assert set(SCHEMAS["UserErasureResult"]["required"]) == keys

    row = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        seq=1,
        created_at=datetime.now(UTC),
        action="user.erase",
        outcome="success",
        actor_type="tenant_key",
        actor_id="abc",
        target_type="user",
        target_id="0" * 64,
        request_id=None,
        status_code=200,
        details={},
        prev_hash="0" * 64,
        row_hash="1" * 64,
    )
    payload = audit_trail.audit_payload(row)
    assert _props("AuditTrailEvent") == set(payload)
    assert set(SCHEMAS["AuditTrailEvent"]["required"]) == set(payload)
    assert _props("AuditTrailActor") == set(payload["actor"])
    assert _props("AuditTrailTarget") == set(payload["target"])


def test_rbac_schemas_match_serializers():
    now = datetime.now(UTC)
    principal = SimpleNamespace(
        id=uuid.uuid4(),
        kind="user",
        external_id="oidc|1",
        email="a@example.com",
        display_name="A",
        created_at=now,
        updated_at=None,
        disabled_at=None,
    )
    keys = set(rbac_admin.serialize_principal(principal))
    assert _props("Principal") == keys == set(SCHEMAS["Principal"]["required"])
    assert _props("PrincipalWithMemberships") == keys | {"memberships"}
    team = SimpleNamespace(
        id=uuid.uuid4(), name="t", description=None, created_at=now, updated_at=None
    )
    keys = set(rbac_admin.serialize_team(team))
    assert _props("Team") == keys == set(SCHEMAS["Team"]["required"])
    project = SimpleNamespace(**vars(team), team_id=uuid.uuid4())
    keys = set(rbac_admin.serialize_project(project))
    assert _props("Project") == keys == set(SCHEMAS["Project"]["required"])
    membership = SimpleNamespace(
        id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        team_id=None,
        project_id=None,
        role="viewer",
        created_by="token:abc",
        created_at=now,
        updated_at=None,
    )
    keys = set(rbac_admin.serialize_membership(membership))
    assert _props("Membership") == keys == set(SCHEMAS["Membership"]["required"])
    assert SCHEMAS["Role"]["enum"] == list(rbac.ROLES)


def test_partner_member_routes_need_members_read():
    for path in ("/partner/v1/teams", "/partner/v1/projects"):
        assert SPEC["paths"][path]["get"]["security"][0] == {"OAuth2": ["members:read"]}
    scopes = SPEC["components"]["securitySchemes"]["OAuth2"]["flows"]["clientCredentials"]["scopes"]
    assert "members:read" in scopes


def test_security_event_schemas_match_serializers():
    row = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        event_id="e-1",
        name="injection.detected",
        occurred_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        status_code=None,
        attributes={},
        **dict.fromkeys(security_events.STRING_COLUMNS.values()),
    )
    payload = security_events.security_event_payload(row)
    assert _props("SecurityEventRow") == set(payload)
    assert set(SCHEMAS["SecurityEventRow"]["required"]) == set(payload)
    result = security_events.IngestResult().as_dict()
    assert _props("SecurityEventIngestResult") == set(result)
    assert set(SCHEMAS["SecurityEventIngestResult"]["required"]) == set(result)
    op = SPEC["paths"]["/partner/v1/security-events"]["get"]
    assert op["security"][0] == {"OAuth2": ["security-events:read"]}
    for path in ("/ingest/security-events", "/ingest/otel/v1/logs"):
        assert SPEC["paths"][path]["post"]["security"] == [{"TenantApiKey": []}]


def test_policy_schemas_match_serializers():
    now = datetime.now(UTC)
    policy = SimpleNamespace(
        id=uuid.uuid4(),
        name="default",
        description=None,
        scope="tenant",
        team_id=None,
        project_id=None,
        active_version=1,
        created_by="token:abc",
        created_at=now,
        updated_at=None,
    )
    keys = set(policies_api.serialize_policy(policy))
    assert _props("Policy") == keys == set(SCHEMAS["Policy"]["required"])
    assert _props("PolicyWithDocument") == keys | {"active_document"}
    version = SimpleNamespace(
        policy_id=uuid.uuid4(),
        version=1,
        sha256="0" * 64,
        document={"schema": "ps-policy.v1", "sections": {}},
        comment=None,
        created_by="token:abc",
        created_at=now,
    )
    keys = set(policies_api.serialize_version_summary(version))
    assert _props("PolicyVersionSummary") == keys == set(
        SCHEMAS["PolicyVersionSummary"]["required"]
    )
    assert _props("PolicyVersion") == set(policies_api.serialize_version(version))
    bundle = policies_api.policy_doc.build_bundle(None, {}, {})
    assert set(SCHEMAS["PolicyBundle"]["required"]) == set(bundle)
    assert set(SCHEMAS["EffectivePolicy"]["required"]) == set(bundle["tenant"])
    assert SCHEMAS["PolicyBundle"]["properties"]["schema"]["const"] == bundle["schema"]
