import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Text, ARRAY, TIMESTAMP, Integer, Numeric, ForeignKey, Index
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
    created_at = Column(TIMESTAMP(timezone=True), default=utcnow)

    assets = relationship("AIAsset", back_populates="tenant")


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
    status = Column(Text, default="active")
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

    asset = relationship("AIAsset", back_populates="usage_events")

    __table_args__ = (
        Index("ix_usage_events_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_usage_events_asset_ts", "asset_id", "timestamp"),
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

    asset = relationship("AIAsset", back_populates="data_flows")

    __table_args__ = (
        Index("ix_data_flows_tenant_asset", "tenant_id", "asset_id"),
    )


class RiskMapping(Base):
    __tablename__ = "risk_mappings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    asset_id = Column(UUID(as_uuid=True), ForeignKey("ai_assets.id"), nullable=False)
    risk_category = Column(Text, nullable=False)
    risk_level = Column(Text, nullable=False)
    framework = Column(Text)

    asset = relationship("AIAsset", back_populates="risk_mappings")

    __table_args__ = (
        Index("ix_risk_mappings_tenant_asset", "tenant_id", "asset_id"),
        Index("ix_risk_mappings_tenant_cat", "tenant_id", "risk_category"),
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
    """Append-only log of every Partner API request.

    Used for compliance, debugging, and tenant-visible activity feeds.
    """

    __tablename__ = "partner_audit_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid7)
    tenant_id = Column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    partner_id = Column(UUID(as_uuid=True), ForeignKey("partner_credentials.id"), nullable=False)
    endpoint = Column(Text, nullable=False)
    method = Column(Text, nullable=False)
    status_code = Column(Integer, nullable=False)
    timestamp = Column(TIMESTAMP(timezone=True), default=utcnow)
    response_count = Column(Integer)
    ip_address = Column(Text)
    user_agent = Column(Text)

    __table_args__ = (
        Index("ix_partner_audit_tenant_ts", "tenant_id", "timestamp"),
        Index("ix_partner_audit_partner_ts", "partner_id", "timestamp"),
    )
