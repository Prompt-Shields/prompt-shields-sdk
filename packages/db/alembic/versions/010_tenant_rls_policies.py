"""Row-level security policies for tenant isolation

Creates the ``tenant_isolation`` policy on every tenant-scoped table (see
db/rls.py). The policies are inert: RLS stays disabled on the tables, so
nothing changes for a running deployment. Enforcement is a separate,
reversible step, taken once every collector instance sets the tenant
context (this release and later):

    python -m collector.rls enable

Downgrade turns enforcement off and drops the policies (#83).

The SQL is inlined (a snapshot of db/rls.py at this revision) so later edits
to db/rls.py don't rewrite history.

Revision ID: 010
Revises: 009
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY = "tenant_isolation"
TENANT_TABLES = {
    "tenants": "id",
    "ai_assets": "tenant_id",
    "ai_usage_events": "tenant_id",
    "data_flows": "tenant_id",
    "risk_mappings": "tenant_id",
    "partner_credentials": "tenant_id",
    "partner_audit_log": "tenant_id",
    "retention_audit_log": "tenant_id",
    "telemetry_client_stats": "tenant_id",
}


def _expr(column: str) -> str:
    return (
        "(coalesce(current_setting('app.rls_bypass', true), '') <> '' "
        f"OR {column} = nullif(current_setting('app.tenant_id', true), '')::uuid)"
    )


def upgrade() -> None:
    for table, column in TENANT_TABLES.items():
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
        op.execute(
            f"CREATE POLICY {POLICY} ON {table} FOR ALL "
            f"USING {_expr(column)} WITH CHECK {_expr(column)}"
        )


def downgrade() -> None:
    for table in TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {table}")
