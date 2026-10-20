"""Hash chain for the admin and security audit trail (``audit_events``).

Each tenant has its own chain. Row ``n`` stores

    prev_hash = row_hash of row n-1   (GENESIS_HASH for seq 1)
    row_hash  = sha256(prev_hash + "\\n" + canonical(row)).hexdigest()

where ``canonical(row)`` is the JSON object of the fields in
``CHAINED_FIELDS`` with sorted keys, no whitespace, UTF-8, UUIDs as
lowercase strings and ``created_at`` as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``
(UTC, microseconds). Changing any chained field of any row, removing a
row from the middle, or reordering rows changes a hash that a later row
depends on, so ``collector.audit_trail.verify_chain`` reports it.

A purge of old rows keeps the rest verifiable with a checkpoint row
(action ``audit.purge``) that records the seq and row_hash of the last
row it removed; the first remaining row's prev_hash must equal it.

Plain Python with no collector imports, so the Alembic backfill (021),
the collector and an external verifier share one definition.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

HASH_VERSION = 1
GENESIS_HASH = "0" * 64

# Chain for events that belong to no tenant (operator authentication
# failures, operator access, read-log purges). Never exported to a tenant.
SYSTEM_TENANT_ID = UUID(int=0)

# Fields covered by row_hash, in addition to prev_hash.
CHAINED_FIELDS: tuple[str, ...] = (
    "id",
    "tenant_id",
    "seq",
    "created_at",
    "action",
    "outcome",
    "actor_type",
    "actor_id",
    "target_type",
    "target_id",
    "request_id",
    "status_code",
    "details",
)

# Action of a checkpoint row written by a purge (see module docstring).
CHECKPOINT_ACTION = "audit.purge"

# pg_advisory_xact_lock(ADVISORY_LOCK_CLASS, hashtext(tenant_id)): one lock
# per tenant chain, held until the writing transaction ends. "PSAU".
ADVISORY_LOCK_CLASS = 0x50534155
ADVISORY_LOCK_SQL = "SELECT pg_advisory_xact_lock(:lock_class, hashtext(:tenant))"

# Transaction-local setting that lets the append-only trigger accept a
# DELETE: the purge sets it to the tenant whose chain prefix it removes.
PURGE_SETTING = "prompt_shields.audit_purge"


def format_ts(value: datetime) -> str:
    """UTC, microsecond precision, "Z" suffix. Naive values are taken as UTC
    (SQLite returns them without a zone)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _plain(value: Any) -> Any:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return format_ts(value)
    return value


def canonical_row(fields: Mapping[str, Any]) -> bytes:
    """Canonical bytes of the chained fields (missing fields are null)."""
    doc = {name: _plain(fields.get(name)) for name in CHAINED_FIELDS}
    doc["v"] = HASH_VERSION
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def compute_row_hash(prev_hash: str, fields: Mapping[str, Any]) -> str:
    return hashlib.sha256(prev_hash.encode() + b"\n" + canonical_row(fields)).hexdigest()


# Append-only on Postgres: no UPDATE ever; DELETE only from the purge.
# The hash chain is what proves integrity (a table owner can disable
# triggers); these stop accidents and casual edits.
NO_UPDATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION audit_events_no_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_events rows are immutable';
END;
$$ LANGUAGE plpgsql
"""
NO_UPDATE_TRIGGER_SQL = """
CREATE TRIGGER audit_events_no_update
BEFORE UPDATE ON audit_events
FOR EACH ROW EXECUTE FUNCTION audit_events_no_update()
"""
NO_DELETE_FUNCTION_SQL = f"""
CREATE OR REPLACE FUNCTION audit_events_no_delete() RETURNS trigger AS $$
BEGIN
    IF current_setting('{PURGE_SETTING}', true) = OLD.tenant_id::text THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'audit_events rows are append-only; only the audit purge may delete them';
END;
$$ LANGUAGE plpgsql
"""
NO_DELETE_TRIGGER_SQL = """
CREATE TRIGGER audit_events_no_delete
BEFORE DELETE ON audit_events
FOR EACH ROW EXECUTE FUNCTION audit_events_no_delete()
"""
TRIGGER_DDL: tuple[str, ...] = (
    NO_UPDATE_FUNCTION_SQL,
    NO_UPDATE_TRIGGER_SQL,
    NO_DELETE_FUNCTION_SQL,
    NO_DELETE_TRIGGER_SQL,
)
