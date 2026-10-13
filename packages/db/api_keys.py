"""Tenant ingest key hashing.

A tenant key is never stored in plaintext. Each tenant row holds:

  - api_key_prefix : first 16 hex chars of SHA-256(key). Indexed, unique,
                     used only to find the candidate row.
  - api_key_hash   : bcrypt hash of the key. The actual check.

Used by the collector's auth, the demo seed and the test fixtures, so they
all write rows the collector can read back.
"""

from __future__ import annotations

import hashlib

import bcrypt

BCRYPT_ROUNDS = 10


def api_key_lookup_prefix(plaintext_key: str) -> str:
    """Indexed lookup ID for a key. Not a secret check on its own."""
    return hashlib.sha256(plaintext_key.encode()).hexdigest()[:16]


def hash_api_key(plaintext_key: str) -> str:
    return bcrypt.hashpw(
        plaintext_key.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    ).decode()


def verify_api_key(plaintext_key: str, hashed: str) -> bool:
    """Constant-time bcrypt comparison. False on a malformed hash."""
    try:
        return bcrypt.checkpw(plaintext_key.encode(), hashed.encode())
    except (ValueError, TypeError):
        return False
