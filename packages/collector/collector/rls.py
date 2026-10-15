"""Tenant context for Postgres row-level security (#83).

The policies (db/rls.py) admit a row only if the transaction setting
``app.tenant_id`` matches its tenant, or ``app.rls_bypass`` is set. This
module sets them.

A session carries its context in ``session.info``; an ``after_begin``
listener copies it into every transaction the session opens with
``set_config(..., true)`` (transaction-local, so nothing leaks to the next
user of a pooled connection). That matters because request handlers commit
more than once (the registry audit write, ingest's rollback-then-record),
and each commit ends the transaction that held the setting.

Three kinds of context:

  ``set_tenant(session, tenant_id)``
      Normal request path. Called by ``auth.resolve_tenant`` and
      ``partner_auth.resolve_partner`` once the caller is authenticated.

  ``auth_lookup(session)``
      Credential lookup before the tenant is known (tenant API key prefix,
      partner client_id or API key). A narrow bypass for the ``async with``
      block only; it is cleared on exit, success or not.

  ``bypass(session, reason)`` / ``bypass_info(reason)``
      Operator routes (PS_ADMIN_TOKEN) and cross-tenant jobs (retention
      purge, erasure, lifecycle sweep, audit purge, reembed, flow rebuild).
      Every grant is logged on the ``collector.rls`` logger with its reason,
      and the reason is the value of ``app.rls_bypass`` for the transaction.

Until RLS is enabled on the tables (``python -m collector.rls enable``) the
settings are set but nothing reads them, so this is safe to deploy first.

CLI (run as the table owner, e.g. the migration role)::

    python -m collector.rls status
    python -m collector.rls enable
    python -m collector.rls disable
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from uuid import UUID

from db import rls as rls_sql
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from sqlalchemy.orm import Session

logger = logging.getLogger("collector.rls")

INFO_KEY = "ps_rls"
AUTH_LOOKUP = "auth-lookup"

_APPLY = text(
    f"SELECT set_config('{rls_sql.TENANT_SETTING}', :tenant, true), "
    f"set_config('{rls_sql.BYPASS_SETTING}', :bypass, true)"
)


@dataclass(frozen=True)
class _Context:
    tenant: str = ""
    bypass: str = ""


_NONE = _Context()


@event.listens_for(Session, "after_begin")
def _apply_on_begin(session: Session, transaction, connection) -> None:
    ctx = session.info.get(INFO_KEY)
    if ctx is not None and ctx != _NONE and connection.dialect.name == "postgresql":
        connection.execute(_APPLY, {"tenant": ctx.tenant, "bypass": ctx.bypass})


async def _set(session: AsyncSession, ctx: _Context) -> None:
    if not isinstance(session, AsyncSession):
        return  # unit-test fakes; a real session always carries the context
    session.info[INFO_KEY] = ctx
    # after_begin only fires for the next transaction; update the open one.
    # (Postgres only: the SQLite unit tests share these code paths.)
    if session.in_transaction() and session.get_bind().dialect.name == "postgresql":
        await session.execute(_APPLY, {"tenant": ctx.tenant, "bypass": ctx.bypass})


async def set_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """Scope the session to one tenant, replacing any other context."""
    await _set(session, _Context(tenant=str(tenant_id)))


async def bypass(session: AsyncSession, reason: str, *, actor: str = "") -> None:
    """Let the session see every tenant's rows. Operator and job paths only."""
    if not reason:
        raise ValueError("an RLS bypass needs a reason")
    logger.info("rls bypass granted reason=%s actor=%s", reason, actor or "-")
    await _set(session, _Context(bypass=reason))


def bypass_info(reason: str) -> dict:
    """``info=`` for a sessionmaker whose sessions all bypass RLS (CLI jobs)."""
    if not reason:
        raise ValueError("an RLS bypass needs a reason")
    logger.info("rls bypass granted reason=%s actor=cli", reason)
    return {INFO_KEY: _Context(bypass=reason)}


async def clear(session: AsyncSession) -> None:
    await _set(session, _NONE)


@asynccontextmanager
async def auth_lookup(session: AsyncSession) -> AsyncIterator[None]:
    """Bypass for a credential lookup, cleared when the block exits.

    ORM objects loaded inside the block (a partner API key lookup loads every
    tenant's active credentials) are expunged on exit, so a later
    ``session.get`` can't hand back another tenant's row from the identity
    map without going through the policies. Their loaded attributes stay
    readable.
    """
    logger.debug("rls bypass granted reason=%s", AUTH_LOOKUP)
    if not isinstance(session, AsyncSession):
        yield  # unit-test fakes
        return
    identity_map = session.sync_session.identity_map
    loaded_before = set(identity_map.keys())
    previous = session.info.get(INFO_KEY, _NONE)
    await _set(session, _Context(bypass=AUTH_LOOKUP))
    try:
        yield
    finally:
        for key in set(identity_map.keys()) - loaded_before:
            obj = identity_map.get(key)
            if obj is not None:
                session.expunge(obj)
        if session.info.get(INFO_KEY) == _Context(bypass=AUTH_LOOKUP):
            try:
                await _set(session, previous)
            except SQLAlchemyError:
                # The transaction has already failed. The next one gets the
                # previous context, because the info no longer holds the bypass.
                session.info[INFO_KEY] = previous


# ──────────────────────────────────────────────────────────────────────
# Status and enable/disable
# ──────────────────────────────────────────────────────────────────────


@dataclass
class TableStatus:
    table: str
    enabled: bool
    forced: bool
    has_policy: bool
    owner: str


@dataclass
class Status:
    role: str
    role_bypasses_rls: bool
    tables: list[TableStatus]
    missing_tables: list[str]

    @property
    def tables_enforced(self) -> bool:
        """Every tenant table has the policy, RLS enabled and FORCEd."""
        return not self.missing_tables and all(
            t.enabled and t.forced and t.has_policy for t in self.tables
        )

    @property
    def enforced(self) -> bool:
        """The tables are enforced and the connecting role is bound by them
        (a superuser or BYPASSRLS role never is)."""
        return self.tables_enforced and not self.role_bypasses_rls


async def status(conn: AsyncConnection) -> Status:
    rows = (
        await conn.execute(
            text(rls_sql.STATUS_QUERY), {"tables": list(rls_sql.TENANT_TABLES)}
        )
    ).all()
    role = (await conn.execute(text(rls_sql.ROLE_QUERY))).one()
    tables = [TableStatus(*row) for row in rows]
    found = {t.table for t in tables}
    return Status(
        role=role[0],
        role_bypasses_rls=bool(role[1] or role[2]),
        tables=tables,
        missing_tables=sorted(set(rls_sql.TENANT_TABLES) - found),
    )


async def enable(conn: AsyncConnection) -> None:
    for stmt in rls_sql.enable_statements():
        await conn.execute(text(stmt))


async def disable(conn: AsyncConnection) -> None:
    for stmt in rls_sql.disable_statements():
        await conn.execute(text(stmt))


async def check_required(conn: AsyncConnection) -> None:
    """Raise if RLS is not enforced for this connection's role
    (PS_RLS_REQUIRED=true runs this at startup)."""
    st = await status(conn)
    if not st.enforced:
        raise RuntimeError(
            "PS_RLS_REQUIRED is set but row-level security is not enforced: "
            + json.dumps(_status_dict(st))
        )


def _status_dict(st: Status) -> dict:
    return {
        "role": st.role,
        "role_bypasses_rls": st.role_bypasses_rls,
        "tables_enforced": st.tables_enforced,
        "enforced": st.enforced,
        "missing_tables": st.missing_tables,
        "tables": [asdict(t) for t in st.tables],
    }


async def _run_cli(command: str) -> dict:
    from sqlalchemy.ext.asyncio import create_async_engine

    from collector.config import settings

    engine = create_async_engine(settings.database_url)
    try:
        async with engine.begin() as conn:
            if command == "enable":
                await enable(conn)
            elif command == "disable":
                await disable(conn)
        async with engine.connect() as conn:
            return _status_dict(await status(conn))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m collector.rls",
        description="Show, enable or disable Postgres row-level security "
        "on the tenant tables. enable/disable must run as the table owner.",
    )
    parser.add_argument("command", choices=("status", "enable", "disable"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(_run_cli(args.command))
    print(json.dumps(result, indent=2))
    if args.command == "enable" and not result["tables_enforced"]:
        return 1
    if result["role_bypasses_rls"]:
        print(
            f"warning: role {result['role']} is a superuser or BYPASSRLS; "
            "policies do not apply to it",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
