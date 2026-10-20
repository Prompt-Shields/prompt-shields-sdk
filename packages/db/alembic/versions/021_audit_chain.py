"""Hash-chained admin and security audit trail (audit_events)

Adds ``audit_events``: one hash chain per tenant of admin and
security-relevant actions (collector/audit_trail.py, db/audit_chain.py),
with ``prev_hash`` / ``row_hash`` and a per-tenant ``seq`` (unique with
the tenant id). No foreign key to ``tenants``, like
``retention_audit_log``: the trail outlives a tenant erasure.

  - Triggers make rows append-only: UPDATE always fails; DELETE only
    inside the audit purge (transaction setting
    ``prompt_shields.audit_purge`` = the chain's tenant id), which leaves an
    ``audit.purge`` checkpoint row so the rest still verifies.
  - The table gets the tenant_isolation policy (same SQL as 015). If RLS is
    already enforced on ai_assets it is enforced here too (same as 020).
  - Backfill: each tenant's existing ``retention_audit_log`` rows (purges,
    erasures, retention changes) become the first rows of its chain, in
    (created_at, id) order, with ``details.backfilled_from`` naming the
    source row. Nothing else existed to backfill: registry reads stay in
    ``partner_audit_log``, which is a separate, unchained read log.
    Tenants with no such rows start their chain at genesis with their next
    audited action.

Revision ID: 021
Revises: 020
Create Date: 2026-10-20
"""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from db import audit_chain
from sqlalchemy.dialects import postgresql

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TABLE = "audit_events"

# retention_audit_log.action -> audit trail action (collector.retention).
BACKFILL_ACTIONS = {
    "purge": "retention.purge",
    "erase": "tenant.erase",
    "set_retention": "tenant.retention.set",
}


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def _backfill(conn) -> None:
    # retention_audit_log may already be under FORCE ROW LEVEL SECURITY:
    # read every tenant's rows (transaction-local, like the collector jobs).
    conn.execute(sa.text("SELECT set_config('app.rls_bypass', 'migration-021', true)"))
    rows = conn.execute(
        sa.text(
            "SELECT id, tenant_id, action, actor, cutoff, details, created_at "
            "FROM retention_audit_log ORDER BY tenant_id, created_at, id"
        )
    ).mappings().all()
    insert = sa.text(
        f"INSERT INTO {TABLE} (id, tenant_id, seq, created_at, action, outcome, "
        "actor_type, actor_id, target_type, target_id, request_id, status_code, "
        "details, prev_hash, row_hash) VALUES (:id, :tenant_id, :seq, :created_at, "
        ":action, :outcome, :actor_type, :actor_id, :target_type, :target_id, "
        ":request_id, :status_code, CAST(:details AS jsonb), :prev_hash, :row_hash)"
    )
    heads: dict = {}
    for r in rows:
        seq, prev = heads.get(r["tenant_id"], (0, audit_chain.GENESIS_HASH))
        details = {k: v for k, v in (r["details"] or {}).items() if isinstance(k, str)}
        if r["cutoff"] is not None:
            details["cutoff"] = audit_chain.format_ts(r["cutoff"])
        details["backfilled_from"] = f"retention_audit_log:{r['id']}"
        fields = {
            "id": uuid.uuid4(),
            "tenant_id": r["tenant_id"],
            "seq": seq + 1,
            "created_at": r["created_at"],
            "action": BACKFILL_ACTIONS.get(r["action"], f"retention.{r['action']}"),
            "outcome": "success",
            "actor_type": "operator" if r["actor"] == "operator-api" else "system",
            "actor_id": r["actor"],
            "target_type": "tenant",
            "target_id": str(r["tenant_id"]),
            "request_id": None,
            "status_code": None,
            "details": details,
        }
        row_hash = audit_chain.compute_row_hash(prev, fields)
        conn.execute(
            insert,
            {
                **fields,
                "details": json.dumps(details),
                "prev_hash": prev,
                "row_hash": row_hash,
            },
        )
        heads[r["tenant_id"]] = (seq + 1, row_hash)
    conn.execute(sa.text("SELECT set_config('app.rls_bypass', '', true)"))


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("outcome", sa.Text, nullable=False),
        sa.Column("actor_type", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text),
        sa.Column("target_type", sa.Text),
        sa.Column("target_id", sa.Text),
        sa.Column("request_id", sa.Text),
        sa.Column("status_code", sa.Integer),
        sa.Column(
            "details",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("prev_hash", sa.Text, nullable=False),
        sa.Column("row_hash", sa.Text, nullable=False),
        sa.UniqueConstraint("tenant_id", "seq", name="uq_audit_events_tenant_seq"),
    )
    op.create_index("ix_audit_events_tenant_ts", TABLE, ["tenant_id", "created_at"])
    op.create_index("ix_audit_events_action_ts", TABLE, ["action", "created_at"])

    # Backfill before the triggers exist (they do not block INSERT anyway).
    _backfill(op.get_bind())

    for stmt in audit_chain.TRIGGER_DDL:
        op.execute(stmt)

    op.execute(
        f"CREATE POLICY {POLICY} ON {TABLE} FOR ALL "
        f"USING {_expr('tenant_id')} WITH CHECK {_expr('tenant_id')}"
    )
    # Match the enforcement state of the existing tenant tables.
    op.execute(
        "DO $$ BEGIN "
        "IF (SELECT relrowsecurity FROM pg_class "
        "WHERE oid = to_regclass('ai_assets')) THEN "
        f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY; "
        f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_action_ts", table_name=TABLE)
    op.drop_index("ix_audit_events_tenant_ts", table_name=TABLE)
    op.drop_table(TABLE)  # drops its triggers and policy
    op.execute("DROP FUNCTION IF EXISTS audit_events_no_update()")
    op.execute("DROP FUNCTION IF EXISTS audit_events_no_delete()")
