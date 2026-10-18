"""Encryption at rest with the PS_WEBHOOK_SECRET_KEYS keyring.

Shared by webhook signing secrets (#103, #170) and provider admin
credentials (#93). One keyring, ``"kid:key,kid:key,..."``: the first entry
encrypts, every entry decrypts what it encrypted.

Tokens are ``"v2.<kid>.<base64(nonce + ciphertext)>"``: AES-256-GCM under a
key derived with HKDF-SHA256 from the keyring entry. Callers pass

  - ``info``: a purpose label fed to HKDF, so each kind of secret gets its
    own derived key (a webhook token never decrypts as a provider
    credential, even for the same row id and key id);
  - ``binding``: bytes authenticated with the ciphertext (normally the row
    id), so a token copied to another row fails to decrypt.

The key id is authenticated too, so a token cannot be relabelled to another
key id. Error messages name key ids, never key material.

The legacy single key (PS_WEBHOOK_SECRET_KEY, "v1." tokens) is a webhook
concern and stays in collector/webhooks.py.
"""

from __future__ import annotations

import base64
import re
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from collector.config import settings

MIN_KEY_LENGTH = 32
KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,32}")


class KeyringNotConfigured(RuntimeError):
    """No usable encryption key: the keyring is unset, a key is too short,
    or the keyring is malformed."""


class SecretKeyMissing(ValueError):
    """A stored secret names a key this process does not have: a key id
    missing from PS_WEBHOOK_SECRET_KEYS, or a legacy "v1." token without
    PS_WEBHOOK_SECRET_KEY.

    Usually a rolling restart that adds a key: processes already running
    the new keyring re-encrypt secrets with it, and the others cannot read
    them until they restart.
    """

    def __init__(self, key_id: str | None) -> None:
        self.key_id = key_id
        name = (
            "legacy key PS_WEBHOOK_SECRET_KEY"
            if key_id is None
            else f"key id {key_id!r}"
        )
        super().__init__(f"secret needs {name}, which this process does not have")


def derive(key: str, info: bytes) -> AESGCM:
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        key.encode()
    )
    return AESGCM(derived)


def keyring() -> list[tuple[str, str]]:
    """(key id, key) pairs from PS_WEBHOOK_SECRET_KEYS, active key first.

    Raises KeyringNotConfigured if the keyring is malformed.
    """
    raw = settings.webhook_secret_keys.strip()
    if not raw:
        return []
    ring: list[tuple[str, str]] = []
    for position, entry in enumerate(raw.split(","), start=1):
        kid, sep, key = (part.strip() for part in entry.partition(":"))
        if not sep or not KEY_ID_RE.fullmatch(kid):
            raise KeyringNotConfigured(
                f"PS_WEBHOOK_SECRET_KEYS entry {position} must be <key id>:<key>, "
                "with a key id of 1-32 characters from A-Z a-z 0-9 _ -"
            )
        if any(kid == seen for seen, _ in ring):
            raise KeyringNotConfigured(f"PS_WEBHOOK_SECRET_KEYS repeats key id {kid!r}")
        if len(key) < MIN_KEY_LENGTH:
            raise KeyringNotConfigured(
                f"PS_WEBHOOK_SECRET_KEYS key {kid!r} must be at least "
                f"{MIN_KEY_LENGTH} characters"
            )
        ring.append((kid, key))
    return ring


def _aad(binding: bytes, kid: str) -> bytes:
    return binding + b"." + kid.encode()


def seal(plaintext: str, binding: bytes, info: bytes) -> str:
    """Encrypt with the active keyring key: "v2.<kid>.<data>".

    Raises KeyringNotConfigured if PS_WEBHOOK_SECRET_KEYS is unset or
    malformed.
    """
    ring = keyring()
    if not ring:
        raise KeyringNotConfigured("PS_WEBHOOK_SECRET_KEYS is not set")
    kid, key = ring[0]
    nonce = secrets.token_bytes(12)
    ct = derive(key, info).encrypt(nonce, plaintext.encode(), _aad(binding, kid))
    return f"v2.{kid}." + base64.urlsafe_b64encode(nonce + ct).decode()


def token_key_id(token: str) -> str | None:
    """Key id a stored token was encrypted with (None: the legacy "v1." key).
    Raises ValueError for an unknown format."""
    version, _, rest = token.partition(".")
    if version == "v1" and rest:
        return None
    if version == "v2":
        kid, _, blob = rest.partition(".")
        if kid and blob:
            return kid
    raise ValueError("unknown secret format")


def unseal(token: str, binding: bytes, info: bytes) -> str:
    """Decrypt a "v2." token with the key it names.

    Raises SecretKeyMissing (a ValueError) if that key is not in the
    keyring, ValueError if the token is malformed or was not sealed for
    this binding and purpose, and KeyringNotConfigured if the keyring
    itself is malformed.
    """
    kid = token_key_id(token)
    if kid is None:
        raise ValueError("not a keyring token")
    key = dict(keyring()).get(kid)
    if key is None:
        raise SecretKeyMissing(kid)
    blob = token.rsplit(".", 1)[1]
    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        pt = derive(key, info).decrypt(raw[:12], raw[12:], _aad(binding, kid))
    except (InvalidTag, ValueError) as exc:  # binascii.Error is a ValueError
        raise ValueError("secret could not be decrypted") from exc
    return pt.decode()


def active_keyring_id() -> str | None:
    """Key id `seal` encrypts with, or None without a keyring.
    Raises KeyringNotConfigured if the keyring is malformed."""
    ring = keyring()
    return ring[0][0] if ring else None
