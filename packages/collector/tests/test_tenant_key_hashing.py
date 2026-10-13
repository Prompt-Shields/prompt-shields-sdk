"""Tenant ingest key hashing and lookup (#81).

No database: request.state.db is a stub that returns whatever row the
prefix lookup would have found.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from collector import auth
from db.api_keys import api_key_lookup_prefix, hash_api_key, verify_api_key
from fastapi import HTTPException

KEY = "ps-test-key-12345"


class _Result:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _DB:
    def __init__(self, row):
        self.row = row
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self.row)


def _request(db, authorization: str | None):
    headers = {} if authorization is None else {"Authorization": authorization}
    return SimpleNamespace(headers=headers, state=SimpleNamespace(db=db))


def _tenant_row(key: str):
    return SimpleNamespace(id=uuid.uuid4(), api_key_hash=hash_api_key(key))


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


class TestApiKeyHelpers:
    def test_hash_is_bcrypt_and_not_plaintext(self):
        hashed = hash_api_key(KEY)
        assert hashed.startswith("$2b$")
        assert KEY not in hashed
        assert verify_api_key(KEY, hashed) is True
        assert verify_api_key(KEY + "x", hashed) is False

    def test_hash_is_salted(self):
        assert hash_api_key(KEY) != hash_api_key(KEY)

    def test_lookup_prefix_is_stable_and_not_plaintext(self):
        prefix = api_key_lookup_prefix(KEY)
        assert prefix == api_key_lookup_prefix(KEY)
        assert len(prefix) == 16
        assert int(prefix, 16) >= 0
        assert prefix != api_key_lookup_prefix(KEY + "x")
        assert "ps-test" not in prefix

    @pytest.mark.parametrize("bad", ["", "not-a-hash", "$2b$10$short"])
    def test_verify_rejects_malformed_hash(self, bad):
        assert verify_api_key(KEY, bad) is False


# ──────────────────────────────────────────────────────────────────────
# resolve_tenant
# ──────────────────────────────────────────────────────────────────────


class TestResolveTenant:
    async def test_valid_key_returns_tenant_id(self):
        row = _tenant_row(KEY)
        tenant_id = await auth.resolve_tenant(_request(_DB(row), f"Bearer {KEY}"))
        assert tenant_id == row.id

    async def test_lookup_filters_on_prefix_not_plaintext(self):
        db = _DB(_tenant_row(KEY))
        await auth.resolve_tenant(_request(db, f"Bearer {KEY}"))
        (stmt,) = db.statements
        params = stmt.compile().params
        assert api_key_lookup_prefix(KEY) in params.values()
        assert KEY not in params.values()
        assert "api_key_prefix" in str(stmt)

    async def test_missing_header_is_401(self):
        with pytest.raises(HTTPException) as exc:
            await auth.resolve_tenant(_request(_DB(None), None))
        assert exc.value.status_code == 401

    async def test_wrong_key_and_unknown_key_get_identical_401(self):
        with pytest.raises(HTTPException) as wrong:
            # Row found by prefix, but the hash belongs to a different key
            await auth.resolve_tenant(
                _request(_DB(_tenant_row("other-key")), f"Bearer {KEY}")
            )
        with pytest.raises(HTTPException) as unknown:
            await auth.resolve_tenant(_request(_DB(None), f"Bearer {KEY}"))
        assert wrong.value.status_code == unknown.value.status_code == 401
        assert wrong.value.detail == unknown.value.detail
        assert wrong.value.detail["detail"] == "Invalid API key"

    async def test_unknown_key_still_runs_bcrypt(self, monkeypatch):
        """No tenant match must cost a bcrypt verify, like a wrong key does."""
        calls = []

        def spy(plaintext, hashed):
            calls.append(hashed)
            return verify_api_key(plaintext, hashed)

        monkeypatch.setattr(auth, "verify_api_key", spy)
        with pytest.raises(HTTPException):
            await auth.resolve_tenant(_request(_DB(None), f"Bearer {KEY}"))
        assert calls == [auth._DUMMY_HASH]

    async def test_dummy_hash_never_matches(self):
        assert verify_api_key("ps-dummy-key-for-constant-time-miss", auth._DUMMY_HASH)
        # A row is required for success, even if a key matched the dummy hash.
        with pytest.raises(HTTPException):
            await auth.resolve_tenant(
                _request(_DB(None), "Bearer ps-dummy-key-for-constant-time-miss")
            )


# ──────────────────────────────────────────────────────────────────────
# Migration 004 hashes the same way the collector checks
# ──────────────────────────────────────────────────────────────────────


def _load_migration():
    pytest.importorskip("alembic")
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "alembic"
        / "versions"
        / "004_hash_tenant_api_keys.py"
    )
    spec = importlib.util.spec_from_file_location("migration_004", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration004:
    def test_revision_chain(self):
        m = _load_migration()
        assert m.revision == "004"
        assert m.down_revision == "003"

    def test_migrated_rows_verify_at_runtime(self):
        m = _load_migration()
        assert m._lookup_prefix(KEY) == api_key_lookup_prefix(KEY)
        assert verify_api_key(KEY, m._bcrypt(KEY))
