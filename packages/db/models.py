import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Text, ARRAY, TIMESTAMP, Boolean, Integer, BigInteger, Numeric, ForeignKey, Index,
    DDL, UniqueConstraint, event,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship
from pgvector.sqlalchemy import Vector


def uuid7():
    """Generate a UUIDv7 (time-sortable). Falls back to uuid4 for Phase 1."""
    return uuid.uuid4()


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    name = Column(Text, nullable=False)
    domain = Column(Text)
    settings = Column(JSONB, default=dict)
    # Ingest key: SHA-256 lookup prefix + bcrypt hash, never plaintext.
    # See db/api_keys.py.
    api_key_prefix = Column(Text)
    api_key_hash = Column(Text)
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)

    assets = relationship("AIAsset", back_populates="tenant")

    __table_args__ = (
        Index("ix_tenants_api_key_prefix", "api_key_prefix", unique=True),
    )


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

    asset = relationship("AIAsset", back_populates="usage_events")

    __table_args__ = (
        Index("ix_usage_events_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_usage_events_asset_ts", "asset_id", "timestamp"),
        Index("ix_usage_events_tenant_key_fp", "tenant_id", "api_key_fingerprint"),
    )


class ProviderCredential(Base):
    """A tenant's provider admin key for the org usage pull (#93).

    `secret_ciphertext` is the key sealed with the PS_WEBHOOK_SECRET_KEYS
    keyring ("v2.<kid>.<data>", AES-GCM, bound to the row id and a provider
    credential purpose label). The key is write-only: the API returns
    `key_hint` (last four characters), the key id and timestamps, never the
    key. One row per tenant and provider. See collector/provider_credentials.py.
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
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
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
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    # NULL for tenant registry reads (no partner involved).
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partner_credentials.id"), nullable=True)
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

# Install the triggers when the schema is built with create_all too (tests).
for _ddl in (
    ATTESTATION_IMMUTABLE_FUNCTION_SQL,
    ATTESTATION_IMMUTABLE_TRIGGER_SQL,
    ATTESTATION_NO_DELETE_FUNCTION_SQL,
    ATTESTATION_NO_DELETE_TRIGGER_SQL,
):
    event.listen(
        AssetAttestation.__table__,
        "after_create",
        DDL(_ddl).execute_if(dialect="postgresql"),
    )
