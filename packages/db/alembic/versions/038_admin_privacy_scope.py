"""Grant admin:privacy to full-scope admin tokens (#353)

The tenant admin scope ``admin:privacy`` (erasure by end user,
collector/user_erasure.py) is new. While PS_ADMIN_REQUIRE_SCOPED_TOKENS is
off, issuing or rotating an ingest key needs every admin scope
(collector.admin_auth.require_full_admin), so a token that held every scope
before this release would lose that ability. This gives ``admin:privacy`` to
every admin token holding all seven scopes that existed before it: those
tokens were full admin and stay full admin. Narrower tokens are unchanged.

Downgrade removes ``admin:privacy`` from every token.

Revision ID: 038
Revises: 037
Create Date: 2026-11-06

036 is taken by an open PR (#338); re-chain onto it if it lands later.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "038"
down_revision: str | None = "037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_SCOPE = "admin:privacy"
# Every admin scope before admin:privacy (frozen here, not imported).
PREVIOUS_SCOPES = (
    "admin:read",
    "admin:keys",
    "admin:tokens",
    "admin:provider-credentials",
    "admin:webhooks",
    "admin:partners",
    "admin:registry",
)


def _bypass() -> None:
    # admin_tokens is under FORCE ROW LEVEL SECURITY (binds the owner too).
    op.execute("SELECT set_config('app.rls_bypass', 'migration-038', true)")


def upgrade() -> None:
    _bypass()
    previous = ",".join(PREVIOUS_SCOPES)
    op.execute(
        f"UPDATE admin_tokens SET scopes = array_append(scopes, '{NEW_SCOPE}') "
        f"WHERE scopes @> '{{{previous}}}'::text[] "
        f"AND NOT ('{NEW_SCOPE}' = ANY(scopes))"
    )


def downgrade() -> None:
    _bypass()
    op.execute(
        f"UPDATE admin_tokens SET scopes = array_remove(scopes, '{NEW_SCOPE}') "
        f"WHERE '{NEW_SCOPE}' = ANY(scopes)"
    )
