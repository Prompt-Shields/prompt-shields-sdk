"""Central policies: deleting a policy keeps its version history (#383)

``DELETE /api/v1/admin/policies/{id}`` used to delete the policy row and,
by cascade, every ``policy_versions`` row, so policy history could be
erased outside tenant teardown (the audit rows keep digests only). Now:

  - ``policies.deleted_at``: a deleted policy is marked, not removed. It is
    left out of listings and the compiled bundle, and its name and binding
    are free again, so the uniqueness rules (one name per tenant, one
    policy per binding) become partial indexes over live policies.
  - ``policy_versions`` and ``policies`` rows can only be deleted by tenant
    erasure (the transaction setting ``prompt_shields.erasing_tenant``, as
    for asset_attestations in 012 and risk_mapping_reviews in 026); any
    other DELETE is refused by a trigger.
  - a deleted policy keeps the team or project it was bound to while they
    exist; deleting that team or project later sets the reference to NULL
    (the foreign keys become ON DELETE SET NULL, and the binding check
    accepts a deleted policy without one). A live policy still blocks the
    delete (collector/rbac_admin.py).

Downgrade removes deleted policies (and their versions) first, since the
old constraints cannot hold them.

Numbered 054 by assignment; chained from 043, main's head when written.
Re-chain onto main's real head at merge.

Revision ID: 054
Revises: 043
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "054"
down_revision: str | None = "043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_BINDING = (
    "(scope = 'tenant' AND team_id IS NULL AND project_id IS NULL) OR "
    "(scope = 'team' AND team_id IS NOT NULL AND project_id IS NULL) OR "
    "(scope = 'project' AND project_id IS NOT NULL AND team_id IS NULL)"
)
# A deleted policy may have lost its team or project (SET NULL).
NEW_BINDING = (
    OLD_BINDING + " OR (deleted_at IS NOT NULL AND team_id IS NULL AND project_id IS NULL)"
)

NO_DELETE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION policies_no_delete() RETURNS trigger AS $$
BEGIN
    IF current_setting('prompt_shields.erasing_tenant', true) = OLD.tenant_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION USING MESSAGE = TG_TABLE_NAME
        || ' rows are kept until tenant erasure; delete a policy through the API (soft delete)';
END;
$$ LANGUAGE plpgsql
"""
NO_DELETE_TRIGGERS = (
    ("policies", "policies_no_delete"),
    ("policy_versions", "policy_versions_no_delete"),
)


def _trigger_sql(table: str, trigger: str) -> str:
    return (
        f"CREATE TRIGGER {trigger} BEFORE DELETE ON {table} "
        "FOR EACH ROW EXECUTE FUNCTION policies_no_delete()"
    )


def _fks(ondelete: str | None) -> None:
    for col, ref in (("team_id", "teams"), ("project_id", "projects")):
        name = f"policies_{col}_fkey"
        op.drop_constraint(name, "policies", type_="foreignkey")
        op.create_foreign_key(name, "policies", ref, [col], ["id"], ondelete=ondelete)


def _unique_indexes(live_only: bool) -> None:
    live = " AND deleted_at IS NULL" if live_only else ""
    for name, cols, where in (
        ("ux_policies_tenant_wide", ["tenant_id"], "scope = 'tenant'"),
        ("ux_policies_team", ["team_id"], "team_id IS NOT NULL"),
        ("ux_policies_project", ["project_id"], "project_id IS NOT NULL"),
    ):
        op.drop_index(name, table_name="policies")
        op.create_index(
            name, "policies", cols, unique=True, postgresql_where=sa.text(where + live)
        )


def upgrade() -> None:
    op.add_column("policies", sa.Column("deleted_at", sa.TIMESTAMP(timezone=True)))
    op.drop_constraint("uq_policies_tenant_name", "policies", type_="unique")
    op.create_index(
        "ux_policies_tenant_name",
        "policies",
        ["tenant_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    _unique_indexes(live_only=True)
    op.drop_constraint("ck_policies_scope_binding", "policies", type_="check")
    op.create_check_constraint("ck_policies_scope_binding", "policies", NEW_BINDING)
    _fks("SET NULL")
    op.execute(NO_DELETE_FUNCTION_SQL)
    for table, trigger in NO_DELETE_TRIGGERS:
        op.execute(_trigger_sql(table, trigger))


def downgrade() -> None:
    for table, trigger in NO_DELETE_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.execute("DROP FUNCTION IF EXISTS policies_no_delete()")
    # The old constraints cannot hold deleted policies (duplicate names or
    # bindings, lost teams): they go, with their versions (cascade).
    op.execute("SELECT set_config('app.rls_bypass', 'migration-054', true)")
    op.execute("DELETE FROM policies WHERE deleted_at IS NOT NULL")
    _fks(None)
    op.drop_constraint("ck_policies_scope_binding", "policies", type_="check")
    op.create_check_constraint("ck_policies_scope_binding", "policies", OLD_BINDING)
    _unique_indexes(live_only=False)
    op.drop_index("ux_policies_tenant_name", table_name="policies")
    op.create_unique_constraint("uq_policies_tenant_name", "policies", ["tenant_id", "name"])
    op.drop_column("policies", "deleted_at")
