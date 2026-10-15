"""Postgres row-level security (RLS) for tenant isolation (#83).

Every tenant-scoped table gets one policy, ``tenant_isolation``, that admits a
row only when either

  - its tenant column equals the transaction setting ``app.tenant_id``, or
  - the transaction setting ``app.rls_bypass`` is non-empty (operator and
    maintenance jobs; the value is the reason, e.g. ``retention-cli``).

Both settings are set with ``set_config(..., is_local => true)``, so they
last for one transaction and never leak to the next user of a pooled
connection. With neither set, every tenant table reads as empty and every
write is rejected. The runtime side (setting them per request or job) lives
in ``collector/rls.py``.

The policies are inert until RLS is switched on for the tables. Migration
014 only creates them; ``enable_statements`` turns enforcement on, with
FORCE so the table owner is bound too (the collector has historically
connected as the owner). Run as the table owner:

    python -m collector.rls enable    # or: status | disable

This module is plain SQL text so the Alembic migration, the CLI and the
tests share one definition.
"""

from __future__ import annotations

TENANT_SETTING = "app.tenant_id"
BYPASS_SETTING = "app.rls_bypass"
POLICY_NAME = "tenant_isolation"

# Table -> column holding the owning tenant's id. A test checks that every
# model with a tenant_id column is listed, so a new table can't ship without
# a policy.
TENANT_TABLES: dict[str, str] = {
    "tenants": "id",
    "ai_assets": "tenant_id",
    "ai_usage_events": "tenant_id",
    "data_flows": "tenant_id",
    "risk_mappings": "tenant_id",
    "partner_credentials": "tenant_id",
    "partner_audit_log": "tenant_id",
    "retention_audit_log": "tenant_id",
    "telemetry_client_stats": "tenant_id",
    "asset_attestations": "tenant_id",
    "webhook_subscriptions": "tenant_id",
    "webhook_deliveries": "tenant_id",
}


def policy_expression(column: str) -> str:
    """Boolean SQL a row must satisfy (for reads and for written rows)."""
    return (
        f"(coalesce(current_setting('{BYPASS_SETTING}', true), '') <> '' "
        f"OR {column} = nullif(current_setting('{TENANT_SETTING}', true), '')::uuid)"
    )


def install_policy_statements() -> list[str]:
    """(Re)create the policy on every tenant table. Idempotent."""
    stmts = []
    for table, column in TENANT_TABLES.items():
        expr = policy_expression(column)
        stmts.append(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
        stmts.append(
            f"CREATE POLICY {POLICY_NAME} ON {table} FOR ALL "
            f"USING {expr} WITH CHECK {expr}"
        )
    return stmts


def drop_policy_statements() -> list[str]:
    return [f"DROP POLICY IF EXISTS {POLICY_NAME} ON {t}" for t in TENANT_TABLES]


def enable_statements() -> list[str]:
    """Install the policies and enforce them, including for the owner."""
    stmts = install_policy_statements()
    for table in TENANT_TABLES:
        stmts.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        stmts.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    return stmts


def disable_statements() -> list[str]:
    """Stop enforcing. The policies stay, so re-enabling is one step."""
    stmts = []
    for table in TENANT_TABLES:
        stmts.append(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        stmts.append(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    return stmts


# One row per tenant table: (table, rls enabled, forced, has policy, owner).
STATUS_QUERY = f"""
SELECT c.relname,
       c.relrowsecurity,
       c.relforcerowsecurity,
       EXISTS (
           SELECT 1 FROM pg_policies p
           WHERE p.tablename = c.relname
             AND p.schemaname = n.nspname
             AND p.policyname = '{POLICY_NAME}'
       ) AS has_policy,
       pg_get_userbyid(c.relowner) AS owner
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname = current_schema()
  AND c.relname = ANY(:tables)
ORDER BY c.relname
"""

# The connecting role. A superuser or BYPASSRLS role ignores every policy.
ROLE_QUERY = """
SELECT current_user, rolsuper, rolbypassrls
FROM pg_roles WHERE rolname = current_user
"""
