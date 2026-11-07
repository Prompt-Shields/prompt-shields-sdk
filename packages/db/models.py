import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    DDL,
    TIMESTAMP,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    event,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship

from db import audit_chain


def uuid7():
    """Generate a UUIDv7 (time-sortable). Falls back to uuid4 for Phase 1."""
    return uuid.uuid4()


def utcnow():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name = Column(Text, nullable=False)
    domain = Column(Text)
    settings = Column(JSONB, default=dict)
    # Legacy single ingest key: SHA-256 lookup prefix + bcrypt hash, never
    # plaintext. See db/api_keys.py. Keys issued through the admin API live
    # in tenant_api_keys; migration 022 (and the first key API call for a
    # tenant) moves a key found here into that table and clears these.
    api_key_prefix = Column(Text)
    api_key_hash = Column(Text)
    # Set by the operator API: the tenant's keys and tokens stop working.
    disabled_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)

    assets = relationship("AIAsset", back_populates="tenant")

    __table_args__ = (
        Index("ix_tenants_api_key_prefix", "api_key_prefix", unique=True),
    )


class TenantSecret(Base):
    """Collector-generated per-tenant secrets (#334), one row per tenant.

    ``user_id_salt`` keys the HMAC in ``ai_usage_events.user_id_hash``
    (collector/user_identity.py). It is inserted once, on the first event
    with a ``user_id`` (``INSERT ... ON CONFLICT DO NOTHING``), and never
    updated, so no writer of ``tenants.settings`` can replace it and break
    hash continuity. No API, export or log reads this table; erasing the
    tenant deletes the row.
    """

    __tablename__ = "tenant_secrets"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)
    user_id_salt = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow, server_default=text("now()"))


class AIAsset(Base):
    __tablename__ = "ai_assets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    vendor = Column(Text, nullable=False)
    model = Column(Text)
    use_case_name = Column(Text)
    business_unit = Column(Text)
    owner_email = Column(Text)
    environment = Column(Text)
    # Cloud region of the deployment (e.g. "swedencentral"), for sovereignty
    # reporting. Set by the cloud inventory platform signal.
    region = Column(Text)
    # What the asset works on (text, audio, image, document), from the first
    # telemetry event that reported one (#75).
    modality = Column(Text)
    status = Column(Text, default="active")
    # Telemetry-driven lifecycle: active -> stale -> retired, back to active
    # when telemetry resumes. Independent of the governance `status` above.
    lifecycle_state = Column(Text, nullable=False, default="active", server_default="active")
    lifecycle_changed_at = Column(TIMESTAMP(timezone=True))
    # Owner-initiated retirement (an `asset` attestation with state
    # `retired`): who retired it. Non-null means the sweep and new telemetry
    # leave lifecycle_state alone. retired_in_use_at is the first sighting
    # after that ("retired asset still in use"). See collector/lifecycle.py.
    retired_by = Column(Text)
    retired_in_use_at = Column(TIMESTAMP(timezone=True))
    data_classification = Column(Text)
    discovery_source = Column(ARRAY(Text), nullable=False)
    confidence = Column(Text, default="low")
    calling_service = Column(Text)
    # pgvector embedding for semantic search (1536 dims = OpenAI text-embedding-3-small)
    embedding = Column(Vector(1536))
    first_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    last_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=utcnow, onupdate=utcnow)

    tenant = relationship("Tenant", back_populates="assets")
    usage_events = relationship("AIUsageEvent", back_populates="asset")
    data_flows = relationship("DataFlow", back_populates="asset")
    risk_mappings = relationship("RiskMapping", back_populates="asset")

    __table_args__ = (
        Index("ix_ai_assets_tenant", "tenant_id"),
        Index("ix_ai_assets_tenant_bu", "tenant_id", "business_unit"),
        Index("ix_ai_assets_tenant_vendor_model", "tenant_id", "vendor", "model"),
        Index("ix_ai_assets_tenant_status", "tenant_id", "status"),
        Index("ix_ai_assets_tenant_lifecycle", "tenant_id", "lifecycle_state"),
        Index("ix_ai_assets_discovery_source", "discovery_source", postgresql_using="gin"),
        Index("ix_ai_assets_embedding", "embedding", postgresql_using="hnsw",
              postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class AIUsageEvent(Base):
    __tablename__ = "ai_usage_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    asset_id = Column(UUID(as_uuid=True), ForeignKey("ai_assets.id"), nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    tokens_in = Column(Integer)
    tokens_out = Column(Integer)
    cost = Column(Numeric(10, 6))
    latency_ms = Column(Integer)
    source = Column(Text, nullable=False)
    session_id = Column(Text)
    tool_calls_used = Column(JSONB)
    prompt_hash = Column(Text)
    # Flow-relevant event metadata (data sources, output destination, PII
    # categories, ...) kept so data flows can be rebuilt. See collector/data_flows.py.
    flow_context = Column(JSONB)
    # Extended usage (#9, #90, #75), all optional; null when not reported.
    # requested_model is what the caller asked for (may be "auto"),
    # served_model what actually ran (dated snapshot, routed model).
    requested_model = Column(Text)
    served_model = Column(Text)
    # Token subsets: tokens_cached (cache reads) and tokens_cache_write are
    # part of tokens_in; tokens_cache_write_1h is part of tokens_cache_write;
    # tokens_reasoning is part of tokens_out.
    tokens_cached = Column(Integer)
    tokens_cache_write = Column(Integer)
    tokens_cache_write_1h = Column(Integer)
    tokens_reasoning = Column(Integer)
    # How `cost` was produced: pricing_table, pricing_table_estimated_tokens,
    # provider_reported, or a framework name when the framework's own cost
    # was used.
    cost_source = Column(Text)
    service_tier = Column(Text)
    # SDK framework integration that reported the call (langchain, ...).
    framework = Column(Text)
    # text, audio, image or document (gateway, from the endpoint).
    modality = Column(Text)
    # Non-token usage: audio seconds, OCR pages, speech characters, ...
    usage_units = Column(Numeric(20, 6))
    usage_unit_type = Column(Text)
    # SDK's sha256(api key)[:16] (#93), never the key. Matches a provider
    # key id through ProviderKeyFingerprint to corroborate platform_signal.
    api_key_fingerprint = Column(Text)
    # Routing, sovereignty and savings metadata (PRD 0001 §7.12, #25), all
    # optional. Metadata only, never prompt or response content.
    # api_surface: the API the caller spoke (chat, messages, responses, ...).
    api_surface = Column(Text)
    # route_mode: gateway, local or none. route_est_cost is the router's
    # estimate in USD before the call.
    route_mode = Column(Text)
    route_group = Column(Text)
    route_reason = Column(Text)
    route_est_cost = Column(Numeric(10, 6))
    # sensitivity_source: declared, pii_escalated or default.
    sensitivity_level = Column(Text)
    sensitivity_source = Column(Text)
    served_provider = Column(Text)
    served_jurisdiction = Column(Text)
    policy_version = Column(Text)
    # Error class of each failed attempt, in order (["rate_limited", ...]).
    # none_as_null: an event without one stores SQL NULL, not JSON null.
    fallback_chain = Column(JSONB(none_as_null=True))
    # What the call would have cost on the baseline model (baseline_id).
    counterfactual_cost = Column(Numeric(10, 6))
    baseline_id = Column(Text)
    # Per-call id from the SDK or gateway (#43): what call feedback names.
    call_id = Column(Text)
    # Spend budgets (#28 gateway, #29 SDK; schemas/budget/README.md), all
    # optional. budget_state: ok, warn, unknown or exceeded; budget_id: the
    # budget that set it; budget_action: none, downgraded or blocked.
    budget_state = Column(Text)
    budget_id = Column(Text)
    budget_action = Column(Text)
    # Gateway cache (#33): the lower-cased cache status (hit, semantic hit,
    # miss, ...) and, on a hit, the upstream cost in USD the hit avoided.
    cache_status = Column(Text)
    cache_saved_cost = Column(Numeric(10, 6))
    # The gateway's estimate of the prompt tokens a cache lookup saved (0 on
    # a miss); summed as cache_tokens_saved in the usage summary.
    est_tokens_saved = Column(Integer)
    # Routing learner auto mode (#34), set when the learner decided the call:
    # route_learner_mode policy, exploit or explore; route_propensity the
    # probability it picked the served model; route_learner_version the
    # active snapshot (lv1-...), null with none active; route_learner_frozen
    # whether learning was frozen at the time.
    route_learner_mode = Column(Text)
    route_propensity = Column(Numeric(7, 6))
    route_learner_version = Column(Text)
    route_learner_frozen = Column(Boolean)
    # Implicit outcome signals (#44): sorted list of known class names
    # ("max_tokens", "refusal", ...; collector.ingest.OUTCOME_SIGNAL_CLASSES),
    # [] when measured and nothing happened, NULL when not reported; and the
    # SDK's OUTCOME_SIGNALS_VERSION that defined them.
    outcome_signals = Column(JSONB(none_as_null=True))
    outcome_signals_version = Column(SmallInteger)
    # Gateway identity and trace fields (#334), all optional. user_id_hash is
    # HMAC-SHA256 of the caller's user_id with the tenant's salt
    # (tenant_secrets.user_id_salt, never returned by any API), stored for
    # every event with a user_id: it is the join key for per-user usage and
    # erasure. user_id_raw is the value as sent, set as well only when the
    # tenant's settings["store_raw_user_ids"] is true. See
    # collector/user_identity.py. user_group: the gateway policy group.
    user_id_hash = Column(Text)
    user_id_raw = Column(Text)
    user_group = Column(Text)
    # Retries and fallbacks: the request's trace id, attempts - 1, and the
    # per-attempt metadata (attempt, provider, model, status, latency_ms,
    # error_class; never content). none_as_null: absent stores SQL NULL.
    trace_id = Column(Text)
    retry_count = Column(Integer)
    attempts = Column(JSONB(none_as_null=True))
    # Streamed responses: upstream closed without a terminal event, and the
    # client disconnected before the stream ended.
    stream_truncated = Column(Boolean)
    stream_cancelled = Column(Boolean)
    # Cost attribution (#347), all optional; see collector/cost_attribution.py.
    # team_id / project_id: the tenant's team and project (#338) the call is
    # attributed to, resolved at ingest (no foreign key: events outlive a
    # deleted team). A project fixes the team, so moving a project later
    # re-attributes future spend only. calling_service: as sent. tags: up to
    # 20 validated {key: value} cost tags; NULL when none.
    team_id = Column(UUID(as_uuid=True))
    project_id = Column(UUID(as_uuid=True))
    calling_service = Column(Text)
    tags = Column(JSONB(none_as_null=True))
    # When the collector stored the row (#346): the database's now() at
    # insert, never sent by a client. The usage rollup job finds the hours
    # to recompute from it (collector/usage_rollups.py).
    ingested_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    asset = relationship("AIAsset", back_populates="usage_events")

    __table_args__ = (
        Index("ix_usage_events_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_usage_events_asset_ts", "asset_id", "timestamp"),
        Index("ix_usage_events_tenant_key_fp", "tenant_id", "api_key_fingerprint"),
        Index("ix_usage_events_tenant_call_id", "tenant_id", "call_id"),
        Index(
            "ix_usage_events_tenant_user_hash",
            "tenant_id",
            "user_id_hash",
            postgresql_where=text("user_id_hash IS NOT NULL"),
        ),
        Index(
            "ix_usage_events_tenant_trace_id",
            "tenant_id",
            "trace_id",
            postgresql_where=text("trace_id IS NOT NULL"),
        ),
        Index("ix_usage_events_tenant_ingested", "tenant_id", "ingested_at"),
    )


class TenantApiKey(Base):
    """A tenant ingest key issued through the admin API.

    Hashed like the legacy key on ``tenants`` (db/api_keys.py): ``key_prefix``
    is the SHA-256 lookup prefix, ``key_hash`` the bcrypt hash. The plaintext
    is returned once, on create and on rotate. ``id`` is stable across
    rotations: a rotation moves the old hash to ``previous_key_*``, which
    keeps authenticating until ``previous_expires_at`` (the grace window).
    DELETE revokes (``revoked_at``); the row stays so audit rows still
    resolve. See collector/tenant_keys.py.
    """

    __tablename__ = "tenant_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name = Column(Text, nullable=False)
    key_prefix = Column(Text, nullable=False)
    key_hash = Column(Text, nullable=False)
    # Last four characters; NULL for a moved legacy key (never known here).
    key_hint = Column(Text)
    previous_key_prefix = Column(Text)
    previous_key_hash = Column(Text)
    previous_expires_at = Column(TIMESTAMP(timezone=True))
    # 'api' (issued here) or 'legacy' (moved from tenants.api_key_*).
    origin = Column(Text, nullable=False, default="api", server_default="api")
    # ingest:write | registry:read | registry:attest (collector/tenant_keys.py).
    # Keys from before migration 037 hold all three; a new row defaults to
    # ingest:write only.
    scopes = Column(
        ARRAY(Text),
        nullable=False,
        default=lambda: ["ingest:write"],
        # A plain literal (Postgres casts it to text[]), so SQLite test
        # schemas can be built from the models too.
        server_default="{ingest:write}",
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    rotated_at = Column(TIMESTAMP(timezone=True))
    expires_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("ux_tenant_api_keys_prefix", "key_prefix", unique=True),
        Index(
            "ux_tenant_api_keys_previous_prefix",
            "previous_key_prefix",
            unique=True,
            postgresql_where=text("previous_key_prefix IS NOT NULL"),
        ),
        # One live key per name; a revoked name can be reused.
        Index(
            "ux_tenant_api_keys_tenant_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_tenant_api_keys_tenant", "tenant_id", "created_at"),
    )


class OperatorToken(Base):
    """A named operator token for /api/v1/operator and the operator-only
    admin routes (#341). Not tied to a tenant: it acts across tenants like
    ``PS_ADMIN_TOKEN``, but with a name, explicit scopes
    (collector/operator_auth.py OPERATOR_SCOPES), an optional expiry and
    revocation, so every action is attributed to it in the audit trail.
    Hashed like the ingest keys and shown once. Names are never reused
    (unique even after revocation), so an audit row's actor id names one
    token for good. Not a tenant table: no row-level security policy.
    """

    __tablename__ = "operator_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name = Column(Text, nullable=False)
    scopes = Column(ARRAY(Text), nullable=False)
    token_prefix = Column(Text, nullable=False)
    token_hash = Column(Text, nullable=False)
    token_hint = Column(Text, nullable=False)
    # Who issued it: "operator" (PS_ADMIN_TOKEN, break-glass) or
    # "operator_token:<name>". Never a secret.
    created_by = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    rotated_at = Column(TIMESTAMP(timezone=True))
    expires_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        Index("ux_operator_tokens_prefix", "token_prefix", unique=True),
        Index("ux_operator_tokens_name", "name", unique=True),
    )


class AdminToken(Base):
    """A tenant-scoped automation token for /api/v1/admin (Terraform, CI).

    Carries explicit scopes (collector/admin_auth.py ADMIN_SCOPES) instead
    of the full access a tenant ingest key has. Hashed like the ingest keys
    and shown once. ``expires_at`` and ``revoked_at`` end it; DELETE
    revokes and keeps the row.
    """

    __tablename__ = "admin_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name = Column(Text, nullable=False)
    scopes = Column(ARRAY(Text), nullable=False)
    token_prefix = Column(Text, nullable=False)
    token_hash = Column(Text, nullable=False)
    token_hint = Column(Text, nullable=False)
    # Who issued it: "operator", or the SHA-256 prefix of the tenant key /
    # admin token that called the API. Never a secret.
    created_by = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    expires_at = Column(TIMESTAMP(timezone=True))
    revoked_at = Column(TIMESTAMP(timezone=True))
    # Issued for a principal (#338): the token then reaches at most what the
    # principal's tenant-wide roles grant now (collector/rbac.py). NULL for
    # automation tokens that belong to no one (every token before 039).
    principal_id = Column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ux_admin_tokens_prefix", "token_prefix", unique=True),
        Index(
            "ux_admin_tokens_tenant_name",
            "tenant_id",
            "name",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_admin_tokens_tenant", "tenant_id", "created_at"),
    )


class Principal(Base):
    """A person or service account in a tenant (#338, RBAC).

    Roles come from ``memberships``; admin tokens and partner credentials
    issued for a principal inherit them (collector/rbac.py). ``disabled_at``
    suspends every credential issued for it without revoking them.
    """

    __tablename__ = "principals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    kind = Column(Text, nullable=False)  # user | service_account
    # The identity provider's subject (OIDC sub, SCIM id); unique per tenant.
    external_id = Column(Text)
    email = Column(Text)
    display_name = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True))
    disabled_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "kind IN ('user', 'service_account')", name="ck_principals_kind"
        ),
        Index(
            "ux_principals_tenant_external_id",
            "tenant_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index("ix_principals_tenant", "tenant_id", "created_at"),
    )


class Team(Base):
    """A group of principals in a tenant (#338)."""

    __tablename__ = "teams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_teams_tenant_name"),
    )


class Project(Base):
    """A project owned by a team (#338)."""

    __tablename__ = "projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id"), nullable=False)
    name = Column(Text, nullable=False)
    description = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_projects_tenant_name"),
        Index("ix_projects_team", "team_id"),
    )


class Membership(Base):
    """A principal's built-in role (collector/rbac.py ROLES) in the tenant
    (``team_id`` NULL), a team, or a project of that team (#338).

    One role per principal per place. Only tenant-wide memberships grant
    scopes on the tenant-wide APIs today; team and project memberships are
    recorded for team-scoped resources.
    """

    __tablename__ = "memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    principal_id = Column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="CASCADE"), nullable=False
    )
    team_id = Column(UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"))
    project_id = Column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE")
    )
    role = Column(Text, nullable=False)
    created_by = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "project_id IS NULL OR team_id IS NOT NULL",
            name="ck_memberships_project_needs_team",
        ),
        Index(
            "ux_memberships_tenant_wide",
            "principal_id",
            unique=True,
            postgresql_where=text("team_id IS NULL"),
        ),
        Index(
            "ux_memberships_team",
            "principal_id",
            "team_id",
            unique=True,
            postgresql_where=text("team_id IS NOT NULL AND project_id IS NULL"),
        ),
        Index(
            "ux_memberships_project",
            "principal_id",
            "project_id",
            unique=True,
            postgresql_where=text("project_id IS NOT NULL"),
        ),
        Index("ix_memberships_tenant", "tenant_id"),
        Index("ix_memberships_team", "team_id"),
    )


class ProviderCredential(Base):
    """A tenant's provider admin key for the org usage pull (#93).

    `secret_ciphertext` is the key sealed with the PS_WEBHOOK_SECRET_KEYS
    keyring ("v2.<kid>.<data>", AES-GCM, bound to the tenant, provider and row
    id and a provider credential purpose label). The key is write-only: the
    API returns `key_hint` (last four characters), the key id and
    timestamps, never the key. One row per tenant and provider. See collector/provider_credentials.py.
    """

    __tablename__ = "provider_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    provider = Column(Text, nullable=False)  # openai | anthropic
    secret_ciphertext = Column(Text, nullable=False)
    key_hint = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    rotated_at = Column(TIMESTAMP(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "provider", name="uq_provider_credentials_tenant_provider"),
    )


class ProviderKeyFingerprint(Base):
    """Links a provider-reported API key id to the SDK's api_key_fingerprint.

    Provider usage APIs report key ids (OpenAI ``key_...``, Anthropic
    ``apikey_...``) and never the key or a hash of it, so the collector
    cannot compute the fingerprint itself; the tenant registers the pair.
    """

    __tablename__ = "provider_key_fingerprints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    provider = Column(Text, nullable=False)
    api_key_id = Column(Text, nullable=False)
    api_key_fingerprint = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "api_key_id", name="uq_provider_key_fingerprints_key"
        ),
        Index("ix_provider_key_fingerprints_tenant_fp", "tenant_id", "api_key_fingerprint"),
    )


class McpToolUsage(Base):
    """Observed MCP tool calls, aggregated (#63, Phase 0: observe only).

    One row per (tenant, source, server, tool, calling service, environment,
    user hash, user group) with call, error and blocked counts and
    first/last seen. Names only: tool arguments, results, descriptions and
    server URLs are never stored. ``user_hash`` is a pseudonymous hex hash
    (the gateway's OIDC subject hash) and ``user_group`` the gateway policy
    group name; "" when not reported.

    ``server_name`` is the server's identity: the name it reports
    (``serverInfo.name``, SDK), the OTel ``server.address`` host (client
    spans) or the server's ``service.name`` (server spans); "unknown" when
    none is known. ``source`` is ``sdk``, ``otel``, ``gateway`` or ``scan``.
    ``calling_service`` and ``environment`` are "" when not reported, so the
    upsert key has no NULLs. Usage is kept apart from ``ai_assets`` (an MCP
    server is not an AI model), but each named server is also registered as
    an asset (vendor ``mcp``, model = ``server_name``) so it has an owner and
    a lifecycle (#77, collector/mcp_usage.py). Rows not seen within the
    tenant's retention period are purged (collector/retention.py).
    """

    __tablename__ = "mcp_tool_usage"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    source = Column(Text, nullable=False)
    server_name = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    calling_service = Column(Text, nullable=False, default="", server_default="")
    environment = Column(Text, nullable=False, default="", server_default="")
    transport = Column(Text)
    call_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    error_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    # Calls a guardrail blocked (gateway); not counted as errors.
    blocked_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    user_hash = Column(Text, nullable=False, default="", server_default="")
    user_group = Column(Text, nullable=False, default="", server_default="")
    # Sum over calls that reported a latency, and how many did (for a mean).
    latency_ms_total = Column(BigInteger, nullable=False, default=0, server_default="0")
    latency_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    first_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    last_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        # Upsert key.
        Index(
            "ux_mcp_tool_usage_key",
            "tenant_id", "source", "server_name", "tool_name", "calling_service",
            "environment", "user_hash", "user_group",
            unique=True,
        ),
        Index("ix_mcp_tool_usage_tenant_last_seen", "tenant_id", "last_seen"),
    )


class McpToolUsageDaily(Base):
    """MCP tool-call metering (#77): one row per (tenant, UTC day, source,
    server, tool) with counts, latency and bytes. Names and numbers only.
    ``bytes_count`` is how many calls reported bytes (the gateway does), so
    a mean can be taken. Rows older than the retention period are purged.
    """

    __tablename__ = "mcp_tool_usage_daily"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    day = Column(Date, nullable=False)
    source = Column(Text, nullable=False)
    server_name = Column(Text, nullable=False)
    tool_name = Column(Text, nullable=False)
    call_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    error_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    blocked_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    latency_ms_total = Column(BigInteger, nullable=False, default=0, server_default="0")
    latency_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    request_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    response_bytes = Column(BigInteger, nullable=False, default=0, server_default="0")
    bytes_count = Column(BigInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index(
            "ux_mcp_tool_usage_daily_key",
            "tenant_id", "day", "source", "server_name", "tool_name",
            unique=True,
        ),
        Index("ix_mcp_tool_usage_daily_tenant_day", "tenant_id", "day"),
    )


class CallFeedback(Base):
    """Explicit feedback on one LLM call (#43), for the routing learner.

    One row per (tenant, call_id): a later submission for the same call
    replaces the earlier one. Metadata only: a score in [0, 1], an optional
    reason from a fixed set of labels (collector/feedback.py), the caller's
    user id as a hex hash (never a raw identifier) and the source. Never
    free text. ``call_id`` is the id the SDK or gateway put on the call's
    usage event; the feedback is kept
    even when that event has not arrived (it may follow). Rows not updated
    within the tenant's retention period are purged with the events.
    """

    __tablename__ = "call_feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    call_id = Column(Text, nullable=False)
    score = Column(Float, nullable=False)
    reason = Column(Text)
    user_id = Column(Text)
    source = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "call_id", name="ux_call_feedback_call"),
        Index("ix_call_feedback_tenant_updated", "tenant_id", "updated_at"),
    )


class LearnerSnapshot(Base):
    """One versioned snapshot of a tenant's routing learner (#47, #48).

    Uploaded by the SDK's offline learner (``ps-learn``, recommend mode) to
    ``POST /ingest/learner-snapshots``. ``learner_version`` is derived from
    the content (SHA-256), so a snapshot is reproducible and checked on
    upload; one row per (tenant, learner_version). ``snapshot`` keeps the
    whole document: config, per-arm statistics over local prompt features
    (aggregates only, never prompt text or embeddings) and recommendations.
    See collector/learner.py.
    """

    __tablename__ = "learner_snapshots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    learner_version = Column(Text, nullable=False)
    parent_version = Column(Text)
    mode = Column(Text, nullable=False)  # recommend
    algorithm = Column(Text, nullable=False)
    feature_schema = Column(Text)
    policy_version = Column(Text)
    data_fingerprint = Column(Text, nullable=False)
    training_rows = Column(Integer, nullable=False)
    scored_rows = Column(Integer, nullable=False)
    snapshot = Column(JSONB, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("tenant_id", "learner_version", name="ux_learner_snapshots_version"),
        Index("ix_learner_snapshots_tenant_created", "tenant_id", "created_at"),
    )


class LearnerRecommendation(Base):
    """A route change the learner proposes for one (use case, sensitivity),
    with its off-policy projection, and the human decision on it (#48).

    ``status``: pending -> accepted | rejected (by a person, with their name
    and an optional comment), or superseded when a newer snapshot proposes
    something for the same group before anyone decided. Accepting never
    changes routing: the reviewer edits the route policy. Realised savings
    after acceptance are computed on read from ``ai_usage_events``.
    """

    __tablename__ = "learner_recommendations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    snapshot_id = Column(
        UUID(as_uuid=True), ForeignKey("learner_snapshots.id"), nullable=False
    )
    learner_version = Column(Text, nullable=False)
    use_case = Column(Text)
    sensitivity_level = Column(Text, nullable=False)
    current_model = Column(Text, nullable=False)
    recommended_model = Column(Text, nullable=False)
    candidates = Column(JSONB, nullable=False)
    projection = Column(JSONB, nullable=False)
    status = Column(Text, nullable=False, default="pending", server_default="pending")
    reviewed_by = Column(Text)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    review_comment = Column(Text)
    reviewer_actor_type = Column(Text)
    reviewer_partner_id = Column(
        UUID(as_uuid=True), ForeignKey("partner_credentials.id"), nullable=True
    )
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_learner_recommendations_tenant_status", "tenant_id", "status"),
        Index("ix_learner_recommendations_snapshot", "snapshot_id"),
    )


class SpendAlert(Base):
    """A spend anomaly (#30): one day's spend for a use case (or asset) far
    above its EWMA baseline. Written by ``python -m collector.spend_anomalies``.

    One row per (tenant, group_by, group_key, day), so re-running the job
    for a day changes nothing. ``group_key`` is the asset's use case name
    (``(none)`` when it has none) or the asset id, per ``group_by``.
    Numbers only: no prompt content, no personal data. ``acknowledged_by``
    names the credential that acknowledged it (``partner:<id>``), never a
    person. Rows for days before the tenant's retention cutoff are purged
    with the events.
    """

    __tablename__ = "spend_alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    group_by = Column(Text, nullable=False)
    group_key = Column(Text, nullable=False)
    day = Column(Date, nullable=False)
    observed_cost = Column(Numeric(14, 6), nullable=False)
    baseline_cost = Column(Float, nullable=False)
    baseline_sigma = Column(Float, nullable=False)
    z_score = Column(Float, nullable=False)
    threshold_sigma = Column(Float, nullable=False)
    history_days = Column(Integer, nullable=False)
    severity = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="open", server_default="open")
    acknowledged_at = Column(TIMESTAMP(timezone=True))
    acknowledged_by = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "group_by", "group_key", "day", name="ux_spend_alerts_group_day"
        ),
        CheckConstraint("status IN ('open', 'acknowledged')", name="ck_spend_alerts_status"),
        Index("ix_spend_alerts_tenant_created", "tenant_id", "created_at"),
        Index("ix_spend_alerts_tenant_day", "tenant_id", "day"),
    )


class GatewaySecurityEvent(Base):
    """A gateway security event (schema v1, docs/security-events.schema.json):
    a guardrail verdict, PII or injection detection, auth failure, policy
    denial, rate limit or payload rejection, ingested by
    ``collector.security_events`` (#353).

    Metadata only, as the gateway sends it: no prompt or response content,
    PII values, credentials or client IP addresses. ``user_hash`` is the
    gateway's pseudonymous OIDC subject hash (policy denials only);
    ``user_id_hash`` is that value hashed with the tenant's user id salt, as
    ``ai_usage_events.user_id_hash`` is, so erasure by the stored hash finds
    it (never returned by the API).
    ``attributes`` holds the event's other ``ps.*`` / ``gen_ai.*`` /
    ``http.*`` fields (scalars only). One row per (tenant, ``event_id``), so
    a resent event is stored once. Rows older than the tenant's retention
    period are purged.
    """

    __tablename__ = "gateway_security_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    event_id = Column(Text, nullable=False)
    occurred_at = Column(TIMESTAMP(timezone=True), nullable=False)
    received_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    name = Column(Text, nullable=False)
    kind = Column(Text)
    outcome = Column(Text)
    severity = Column(Text)
    action = Column(Text)
    signature = Column(Text)
    request_id = Column(Text)
    route = Column(Text)
    method = Column(Text)
    business_unit = Column(Text)
    environment = Column(Text)
    provider = Column(Text)
    model = Column(Text)
    status_code = Column(Integer)
    user_hash = Column(Text)
    user_id_hash = Column(Text)
    schema_version = Column(Text)
    attributes = Column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("tenant_id", "event_id", name="ux_gateway_security_events_event"),
        Index("ix_gateway_security_events_tenant_occurred", "tenant_id", "occurred_at"),
        Index("ix_gateway_security_events_tenant_received", "tenant_id", "received_at", "id"),
        Index(
            "ix_gateway_security_events_tenant_user",
            "tenant_id",
            "user_id_hash",
            postgresql_where=text("user_id_hash IS NOT NULL"),
        ),
    )


def _rollup_count(name: str) -> Column:
    return Column(name, BigInteger, nullable=False, default=0, server_default="0")


class _UsageRollupColumns:
    """Columns shared by the hourly and daily usage rollups (#346).

    One row per (tenant, bucket, asset, model, provider, API surface, user
    hash, team, project, calling service, cost tags). The dimensions are null when the events did not report them.
    ``model`` is the served model, else the requested one; ``provider`` the
    served provider; ``user_id_hash`` the HMAC of the caller's user id
    (never the raw id). Sums of the events' columns, so a rollup is exactly
    the raw events it was computed from. Numbers and hashes only.
    """

    id = Column(BigInteger, Identity(), primary_key=True)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    # UTC start of the hour (hourly) or day (daily).
    bucket_start = Column(TIMESTAMP(timezone=True), nullable=False)
    # No foreign key: rollups outlive raw events, and an asset can be merged
    # into another (asset_merge repoints these too).
    asset_id = Column(UUID(as_uuid=True))
    model = Column(Text)
    provider = Column(Text)
    api_surface = Column(Text)
    user_id_hash = Column(Text)
    # Cost attribution (#347): the events' team, project and calling service,
    # and their tags restricted to the tenant's settings.cost_tag_keys (NULL
    # when none of those keys is set), so tag cardinality stays bounded.
    team_id = Column(UUID(as_uuid=True))
    project_id = Column(UUID(as_uuid=True))
    calling_service = Column(Text)
    tags = Column(JSONB(none_as_null=True))
    requests = _rollup_count("requests")
    tokens_in = _rollup_count("tokens_in")
    tokens_out = _rollup_count("tokens_out")
    tokens_cached = _rollup_count("tokens_cached")
    tokens_cache_write = _rollup_count("tokens_cache_write")
    tokens_reasoning = _rollup_count("tokens_reasoning")
    # Sum of cost over the events that reported one (cost_requests).
    cost = Column(Numeric(20, 6), nullable=False, default=0, server_default="0")
    cost_requests = _rollup_count("cost_requests")
    cache_saved_cost = Column(Numeric(20, 6), nullable=False, default=0, server_default="0")
    cache_lookups = _rollup_count("cache_lookups")
    cache_hits = _rollup_count("cache_hits")
    # Events that reported per-attempt metadata, and those whose last
    # attempt failed (HTTP status >= 400): the error rate's two sides.
    attempts_reported = _rollup_count("attempts_reported")
    error_requests = _rollup_count("error_requests")


class UsageRollupHourly(_UsageRollupColumns, Base):
    """Hourly usage rollup (#346), kept PS_USAGE_ROLLUP_RETENTION_DAYS."""

    __tablename__ = "usage_rollup_hourly"
    __table_args__ = (Index("ix_usage_rollup_hourly_tenant_bucket", "tenant_id", "bucket_start"),)


class UsageRollupDaily(_UsageRollupColumns, Base):
    """Daily usage rollup (#346): the sum of the day's hourly rows."""

    __tablename__ = "usage_rollup_daily"
    __table_args__ = (Index("ix_usage_rollup_daily_tenant_bucket", "tenant_id", "bucket_start"),)


class UsageRollupState(Base):
    """Per-tenant progress of the usage rollup job (#346): ``watermark`` is
    the database time of its last run; the next run recomputes the buckets
    of events ingested since then (less an overlap)."""

    __tablename__ = "usage_rollup_state"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)
    watermark = Column(TIMESTAMP(timezone=True))
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)


class DataFlow(Base):
    __tablename__ = "data_flows"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    asset_id = Column(UUID(as_uuid=True), ForeignKey("ai_assets.id"), nullable=False)
    source_system = Column(Text, nullable=False)
    destination_system = Column(Text, nullable=False)
    data_classification = Column(Text)
    direction = Column(Text, nullable=False)
    detected_pii_types = Column(JSONB)
    # Derived from telemetry (collector/data_flows.py). Hand-entered rows keep
    # event_count 0 and null first/last seen.
    event_count = Column(BigInteger, nullable=False, default=0, server_default="0")
    first_seen = Column(TIMESTAMP(timezone=True))
    last_seen = Column(TIMESTAMP(timezone=True))

    asset = relationship("AIAsset", back_populates="data_flows")

    __table_args__ = (
        Index("ix_data_flows_tenant_asset", "tenant_id", "asset_id"),
        # Upsert key for derived flows.
        Index(
            "ux_data_flows_flow_key",
            "tenant_id", "asset_id", "source_system", "destination_system", "direction",
            unique=True,
        ),
    )


class RiskMapping(Base):
    __tablename__ = "risk_mappings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    asset_id = Column(UUID(as_uuid=True), ForeignKey("ai_assets.id"), nullable=False)
    risk_category = Column(Text, nullable=False)
    risk_level = Column(Text, nullable=False)
    framework = Column(Text)
    # Rule-based suggestions (collector/risk_suggestions.py, #97). Hand-entered
    # rows are source 'manual', status 'confirmed'. The engine writes source
    # 'rules', status 'suggested' and only ever changes or withdraws rows that
    # are still 'suggested'; a human review sets 'confirmed' or 'rejected'.
    status = Column(Text, nullable=False, default="confirmed", server_default="confirmed")
    source = Column(Text, nullable=False, default="manual", server_default="manual")
    rule_id = Column(Text)
    ruleset_version = Column(Text)
    framework_ref = Column(Text)
    confidence = Column(Text)
    # Which signals triggered the rule: [{signal, field, value, matched, role}].
    evidence = Column(JSONB)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), default=utcnow)
    reviewed_at = Column(TIMESTAMP(timezone=True))
    # Who reviewed (a person, e.g. an email) and the tenant key prefix used.
    reviewed_by = Column(Text)
    reviewed_key_prefix = Column(Text)
    # The reviewer's comment on the latest review. Every review is also kept
    # in risk_mapping_reviews (append-only history).
    review_comment = Column(Text)

    asset = relationship("AIAsset", back_populates="risk_mappings")

    __table_args__ = (
        Index("ix_risk_mappings_tenant_asset", "tenant_id", "asset_id"),
        Index("ix_risk_mappings_tenant_cat", "tenant_id", "risk_category"),
        Index("ix_risk_mappings_tenant_status", "tenant_id", "status"),
        # One row per rule per asset; the engine's upsert key.
        Index(
            "ux_risk_mappings_rule",
            "tenant_id", "asset_id", "rule_id",
            unique=True,
            postgresql_where=rule_id.isnot(None),
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Partner API — credentials and audit log
# ──────────────────────────────────────────────────────────────────────


class PartnerCredential(Base):
    """OAuth client + optional API key for EA partner integrations.

    One row per (tenant, partner). `client_secret_hash` and `api_key_hash`
    are bcrypt hashes — plaintext is shown only once on creation.
    """

    __tablename__ = "partner_credentials"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    partner_name = Column(Text, nullable=False)
    client_id = Column(Text, nullable=False, unique=True)
    client_secret_hash = Column(Text, nullable=False)
    api_key_hash = Column(Text)
    scopes = Column(ARRAY(Text), nullable=False, default=lambda: ["registry:read"])
    rate_limit = Column(Integer, default=1000)
    # Named tier from collector.partner_scopes.RATE_LIMIT_TIERS; NULL means
    # the custom `rate_limit` above applies (all Phase 1 credentials).
    rate_limit_tier = Column(Text)
    is_active = Column(Text, default="true")  # 'true' | 'false' — text for portability
    partner_metadata = Column(JSONB, default=dict)
    expires_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)
    # Issued for a principal (#338): like AdminToken.principal_id.
    principal_id = Column(
        UUID(as_uuid=True), ForeignKey("principals.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_partner_credentials_tenant", "tenant_id"),
        Index("ix_partner_credentials_client_id", "client_id"),
        Index("ix_partner_credentials_name", "partner_name"),
    )


class PartnerAuditLog(Base):
    """Append-only log of every registry read.

    Covers both the Partner API (`partner_id` set) and the tenant registry
    `/api/v1/registry/*` (`partner_id` NULL, `api_key_prefix` set). Written
    by `collector.audit.record_read`. Used for compliance, debugging, and
    tenant-visible activity feeds.
    """

    __tablename__ = "partner_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # NULL for tenant registry reads (no partner involved).
    partner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("partner_credentials.id", ondelete="CASCADE"),
        nullable=True,
    )
    # SHA-256 lookup prefix of the caller's API key, never the key itself.
    api_key_prefix = Column(Text)
    endpoint = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    status_code = Column(Integer, nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), default=utcnow)
    response_count = Column(Integer)
    # Asset ids in the path or returned by the read (capped).
    asset_ids = Column(ARRAY(UUID(as_uuid=True)))
    # Query parameters with secrets redacted and free-text search hashed.
    query_params = Column(JSONB)
    ip_address = Column(Text)
    user_agent = Column(Text)

    __table_args__ = (
        Index("ix_partner_audit_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_partner_audit_partner_ts", "partner_id", "timestamp"),
        Index("ix_partner_audit_ts", "timestamp"),
    )


# ──────────────────────────────────────────────────────────────────────
# Data retention — purge and erasure audit trail (#85)
# ──────────────────────────────────────────────────────────────────────


class RetentionAuditLog(Base):
    """Append-only record of every retention purge, tenant erasure and
    retention-setting change.

    `tenant_id` deliberately has no foreign key: the row has to outlive the
    tenant it describes, so an erasure stays provable after the tenant is gone.
    `details` holds counts (or the new setting), never the deleted data.
    """

    __tablename__ = "retention_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), nullable=False)
    action = Column(Text, nullable=False)  # 'purge' | 'erase' | 'set_retention'
    actor = Column(Text, nullable=False)
    cutoff = Column(TIMESTAMP(timezone=True))
    details = Column(JSONB, default=dict)  # row counts per table, or the new setting
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_retention_audit_tenant_ts", "tenant_id", "created_at"),
    )


class AuditEvent(Base):
    """Tamper-evident trail of admin and security-relevant actions.

    One hash chain per tenant (``seq`` 1, 2, ...; see db/audit_chain.py):
    credential and scope changes, webhook admin, operator actions,
    retention runs, registry lifecycle changes and admin auth failures.
    Written by ``collector.audit_trail.record`` in the same transaction as
    the change. Holds ids and counts only: never a secret, never personal
    data beyond ids (no emails, IP addresses or free text).

    ``tenant_id`` has no foreign key, like ``retention_audit_log``: the
    chain outlives a tenant erasure. Events with no tenant use
    ``SYSTEM_TENANT_ID`` (the nil UUID). Rows are append-only on Postgres
    (triggers reject UPDATE, and DELETE outside the audit purge).
    """

    __tablename__ = "audit_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), nullable=False)
    seq = Column(BigInteger, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    action = Column(Text, nullable=False)  # e.g. provider_credential.rotate
    outcome = Column(Text, nullable=False)  # success | failure
    actor_type = Column(Text, nullable=False)  # tenant_key | partner | operator | system
    # Key prefix, partner credential id or operator/job name; never a secret.
    actor_id = Column(Text)
    target_type = Column(Text)
    target_id = Column(Text)
    request_id = Column(Text)
    status_code = Column(Integer)
    details = Column(JSONB, nullable=False, default=dict)
    prev_hash = Column(Text, nullable=False)
    row_hash = Column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "seq", name="uq_audit_events_tenant_seq"),
        # Chains start at 1; verify_chain also flags rows below it (024).
        CheckConstraint("seq >= 1", name="ck_audit_events_seq_positive"),
        Index("ix_audit_events_tenant_ts", "tenant_id", "created_at"),
        Index("ix_audit_events_action_ts", "action", "created_at"),
    )


class TelemetryClientStats(Base):
    """Per-tenant, per-SDK-client delivery accounting (#87).

    One row per (tenant, client_id). client_id is the random id an SDK sends
    in batch metadata; batches without metadata (older SDKs, other callers)
    land on client_id "unidentified". Counters are cumulative:

      received_events / received_batches  stored by the collector
      sdk_enqueued_total, sdk_dropped     latest cumulative figures the SDK
                                          reported (only taken from a batch
                                          whose seq is newer than last_seq)
      collector_rejected                  events in batches the collector
                                          refused or failed to store, by
                                          reason (the SDK normally retries)
      ignored_fields                      unknown event fields the collector
                                          discarded, field name -> count
    """

    __tablename__ = "telemetry_client_stats"

    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), primary_key=True)
    client_id = Column(Text, primary_key=True)
    sdk = Column(Text)
    last_seq = Column(BigInteger, nullable=False, default=0)
    received_events = Column(BigInteger, nullable=False, default=0)
    received_batches = Column(BigInteger, nullable=False, default=0)
    sdk_enqueued_total = Column(BigInteger, nullable=False, default=0)
    sdk_dropped = Column(JSONB, nullable=False, default=dict)
    collector_rejected = Column(JSONB, nullable=False, default=dict)
    ignored_fields = Column(JSONB, nullable=False, default=dict)
    first_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    last_seen = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)


# ──────────────────────────────────────────────────────────────────────
# Partner change webhooks (#103)
# ──────────────────────────────────────────────────────────────────────


class WebhookSubscription(Base):
    """A partner's HTTPS endpoint for asset change events.

    Created by a tenant admin for one of the tenant's partner credentials.
    `secret_ciphertext` is the signing secret encrypted with a key from
    PS_WEBHOOK_SECRET_KEYS or the legacy PS_WEBHOOK_SECRET_KEY (AES-GCM,
    bound to the row id; the token names its key id); the plaintext is
    returned once, on creation or rotation. After a rotation with a grace
    period, `previous_secret_ciphertext` holds the replaced secret, which
    also signs deliveries until `previous_secret_expires_at`. See
    collector/webhooks.py.
    """

    __tablename__ = "webhook_subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    partner_id = Column(
        UUID(as_uuid=True),
        ForeignKey("partner_credentials.id", ondelete="CASCADE"),
        nullable=False,
    )
    url = Column(Text, nullable=False)
    event_types = Column(ARRAY(Text), nullable=False)
    description = Column(Text)
    secret_ciphertext = Column(Text, nullable=False)
    previous_secret_ciphertext = Column(Text)
    previous_secret_expires_at = Column(TIMESTAMP(timezone=True))
    is_active = Column(Boolean, nullable=False, default=True, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    updated_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)

    __table_args__ = (
        Index("ix_webhook_subscriptions_tenant", "tenant_id"),
        Index("ix_webhook_subscriptions_partner", "partner_id"),
    )


class WebhookDelivery(Base):
    """One event for one subscription: the outbox row and its delivery state.

    status: pending (waiting for an attempt), succeeded, or dead (gave up;
    shown in the dead-letter view and can be redelivered). The payload is
    built when the event is enqueued and holds no owner email or content.
    """

    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    subscription_id = Column(
        UUID(as_uuid=True),
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_id = Column(UUID(as_uuid=True), nullable=False)
    event_type = Column(Text, nullable=False)
    asset_id = Column(UUID(as_uuid=True))
    payload = Column(JSONB, nullable=False)
    status = Column(Text, nullable=False, default="pending", server_default="pending")
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)
    last_attempt_at = Column(TIMESTAMP(timezone=True))
    last_status_code = Column(Integer)
    last_error = Column(Text)
    delivered_at = Column(TIMESTAMP(timezone=True))
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_tenant_status", "tenant_id", "status", "created_at"),
        Index(
            "ux_webhook_deliveries_sub_event", "subscription_id", "event_id", unique=True
        ),
    )


# ──────────────────────────────────────────────────────────────────────
# Human attestation (#98)
# ──────────────────────────────────────────────────────────────────────


class AssetAttestation(Base):
    """Append-only history of human sign-offs on an asset field.

    One row per decision: who (`attested_by`, plus the authenticated caller),
    when, which field, the state chosen and a snapshot of the field's value
    at that moment. The current state of a field is its newest row. Rows are
    never updated or deleted (triggers reject UPDATE, and DELETE outside
    tenant erasure, on Postgres). Attestation never changes `AIAsset.confidence`.
    See collector/attestation.py.
    """

    __tablename__ = "asset_attestations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    asset_id = Column(UUID(as_uuid=True), ForeignKey("ai_assets.id"), nullable=False)
    field = Column(Text, nullable=False)  # e.g. 'asset', 'owner', 'data_classification'
    state = Column(Text, nullable=False)  # unreviewed | attested | disputed | retired
    value = Column(JSONB)  # the field's value when the decision was made
    comment = Column(Text)
    attested_by = Column(Text, nullable=False)  # the human, e.g. an email
    actor_type = Column(Text, nullable=False)  # 'tenant' | 'partner'
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partner_credentials.id"), nullable=True)
    # SHA-256 lookup prefix of the tenant API key used, never the key itself.
    api_key_prefix = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index(
            "ix_asset_attestations_tenant_asset_field_ts",
            "tenant_id", "asset_id", "field", "created_at",
        ),
    )


ATTESTATION_IMMUTABLE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION asset_attestations_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'asset_attestations rows are immutable; insert a new row instead';
END;
$$ LANGUAGE plpgsql
"""
ATTESTATION_IMMUTABLE_TRIGGER_SQL = """
CREATE TRIGGER asset_attestations_no_update
BEFORE UPDATE ON asset_attestations
FOR EACH ROW EXECUTE FUNCTION asset_attestations_immutable()
"""
# DELETE is allowed only for tenant erasure, which sets this transaction-local
# setting to the tenant being erased (collector/retention.py). Any other
# DELETE would let history be rewritten (delete, then insert a new row).
ATTESTATION_ERASE_SETTING = "prompt_shields.erasing_tenant"
ATTESTATION_NO_DELETE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION asset_attestations_no_delete() RETURNS trigger AS $$
BEGIN
    IF current_setting('prompt_shields.erasing_tenant', true) = OLD.tenant_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'asset_attestations rows are append-only; only tenant erasure may delete them';
END;
$$ LANGUAGE plpgsql
"""
ATTESTATION_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER asset_attestations_no_delete
BEFORE DELETE ON asset_attestations
FOR EACH ROW EXECUTE FUNCTION asset_attestations_no_delete()
"""

class RiskMappingReview(Base):
    """Append-only history of human reviews of a risk mapping (#98).

    One row per decision: who (`reviewed_by`, plus the authenticated tenant
    key, admin token or partner), when, the status chosen, the status it
    replaced, a comment and a snapshot of the mapping (category, level,
    framework, rule) at that moment. `risk_mappings` keeps the latest
    decision. Rows are never updated or deleted outside tenant erasure
    (triggers, as on asset_attestations). `risk_mapping_id` and `asset_id`
    have no foreign key on purpose: a duplicate-asset merge can drop a
    mapping or re-point it to another asset, and the history must outlive
    both. See collector/risk_reviews.py.
    """

    __tablename__ = "risk_mapping_reviews"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    risk_mapping_id = Column(UUID(as_uuid=True), nullable=False)
    asset_id = Column(UUID(as_uuid=True), nullable=False)  # at review time
    status = Column(Text, nullable=False)  # confirmed | rejected
    previous_status = Column(Text)  # suggested | confirmed | rejected
    comment = Column(Text)
    reviewed_by = Column(Text, nullable=False)  # the human, e.g. an email
    actor_type = Column(Text, nullable=False)  # 'tenant_key' | 'admin_token' | 'partner'
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partner_credentials.id"), nullable=True)
    # SHA-256 lookup prefix of the tenant key / admin token, never the secret.
    api_key_prefix = Column(Text)
    # The mapping as reviewed: risk_category, risk_level, framework, rule_id,
    # ruleset_version, framework_ref.
    snapshot = Column(JSONB)
    created_at = Column(TIMESTAMP(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index(
            "ix_risk_mapping_reviews_tenant_mapping_ts",
            "tenant_id", "risk_mapping_id", "created_at",
        ),
    )


RISK_REVIEW_IMMUTABLE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION risk_mapping_reviews_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'risk_mapping_reviews rows are immutable; insert a new row instead';
END;
$$ LANGUAGE plpgsql
"""
RISK_REVIEW_IMMUTABLE_TRIGGER_SQL = """
CREATE TRIGGER risk_mapping_reviews_no_update
BEFORE UPDATE ON risk_mapping_reviews
FOR EACH ROW EXECUTE FUNCTION risk_mapping_reviews_immutable()
"""
# Same erasure exception as asset_attestations (ATTESTATION_ERASE_SETTING).
RISK_REVIEW_NO_DELETE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION risk_mapping_reviews_no_delete() RETURNS trigger AS $$
BEGIN
    IF current_setting('prompt_shields.erasing_tenant', true) = OLD.tenant_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'risk_mapping_reviews rows are append-only; only tenant erasure may delete them';
END;
$$ LANGUAGE plpgsql
"""
RISK_REVIEW_NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER risk_mapping_reviews_no_delete
BEFORE DELETE ON risk_mapping_reviews
FOR EACH ROW EXECUTE FUNCTION risk_mapping_reviews_no_delete()
"""

# Install the triggers when the schema is built with create_all too (tests).
for _table, _ddls in (
    (
        RiskMappingReview.__table__,
        (
            RISK_REVIEW_IMMUTABLE_FUNCTION_SQL,
            RISK_REVIEW_IMMUTABLE_TRIGGER_SQL,
            RISK_REVIEW_NO_DELETE_FUNCTION_SQL,
            RISK_REVIEW_NO_DELETE_TRIGGER_SQL,
        ),
    ),
    (
        AssetAttestation.__table__,
        (
            ATTESTATION_IMMUTABLE_FUNCTION_SQL,
            ATTESTATION_IMMUTABLE_TRIGGER_SQL,
            ATTESTATION_NO_DELETE_FUNCTION_SQL,
            ATTESTATION_NO_DELETE_TRIGGER_SQL,
        ),
    ),
    (AuditEvent.__table__, audit_chain.TRIGGER_DDL),
):
    for _ddl in _ddls:
        event.listen(_table, "after_create", DDL(_ddl).execute_if(dialect="postgresql"))

