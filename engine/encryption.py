"""
Encryption service — AES-256 at-rest encryption for everything the app
persists to disk.

Design notes (why it's split this way):

  * `EncryptionService` is the only thing the storage layer depends on.
    It knows nothing about files, projects, or JSON — just "bytes in,
    bytes out". This keeps the crypto swappable without touching a
    single line of `engine/storage.py`.

  * `KeyProvider` is a separate interface from `EncryptionService` on
    purpose. Today there's exactly one implementation
    (`LocalKeyProvider`), which keeps a random 256-bit key in a local
    key file so encryption is fully transparent — the user never sees
    a password. Tomorrow, a `PasswordKeyProvider` that derives the key
    from a user passphrase (via a KDF such as Scrypt/PBKDF2) can be
    dropped in without changing `AesGcmEncryptionService` or storage.py
    at all — that's the extensibility point called for in the
    requirements.

  * AES-256-**GCM** (authenticated encryption), not plain CBC. GCM gives
    both confidentiality and integrity: a tampered/corrupted ciphertext
    fails to decrypt instead of silently returning garbage. This is the
    encryption itself, not an encoding trick — nothing here relies on
    Base64 or obfuscation for security; Base64 is never used as part of
    the security boundary (see `encrypt_to_disk_bytes` — payloads are
    written as raw bytes).

  * File format on disk (magic header lets us tell "already encrypted"
    apart from legacy plaintext, which is what the migration path in
    storage.py uses):

        b"ASWENC1" | nonce (12 bytes) | ciphertext+tag (AES-GCM output)

    "ASWENC1" = AI Story Writer ENCryption format version 1.
"""

from __future__ import annotations

import abc
import logging
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("encryption")

MAGIC = b"ASWENC1"
_KEY_SIZE_BYTES = 32
_NONCE_SIZE_BYTES = 12


class DecryptionError(Exception):
    """Raised when data cannot be decrypted (wrong key, corruption, or tampering)."""






class KeyProvider(abc.ABC):
    """
    Supplies the raw symmetric key used for encryption.

    Kept separate from EncryptionService so the *source* of the key
    (a local key file today, a user password tomorrow) can change
    without the encryption/decryption logic ever knowing the
    difference.
    """

    @abc.abstractmethod
    def get_key(self) -> bytes:
        """Return the 32-byte (256-bit) AES key."""
        raise NotImplementedError


class LocalKeyProvider(KeyProvider):
    """
    Generates a random AES-256 key on first use and persists it to a
    local key file, reusing it on every subsequent run. This is what
    makes encryption fully transparent for the current "no user
    password" version of the app: there's nothing for the user to
    remember or type.

    The key file is created with owner-only permissions (0600) on
    POSIX systems as basic hardening; it's still a local secret file,
    not a substitute for a real password-derived key, which is exactly
    why `PasswordKeyProvider` (see class docstring below) exists as the
    designed upgrade path.
    """

    def __init__(self, key_file: Path) -> None:
        self._key_file = key_file
        self._key: bytes | None = None

    def get_key(self) -> bytes:
        if self._key is not None:
            return self._key
        self._key_file.parent.mkdir(parents=True, exist_ok=True)
        if self._key_file.exists():
            key = self._key_file.read_bytes()
            if len(key) != _KEY_SIZE_BYTES:
                raise DecryptionError(
                    f"Key file '{self._key_file}' is corrupt (expected "
                    f"{_KEY_SIZE_BYTES} bytes, got {len(key)})."
                )
        else:
            key = os.urandom(_KEY_SIZE_BYTES)
            self._key_file.write_bytes(key)
            try:
                os.chmod(self._key_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:


                pass
            logger.info(f"Generated new local encryption key at '{self._key_file}'.")
        self._key = key
        return key


class PasswordKeyProvider(KeyProvider):
    """
    Future extensibility point: derive the AES-256 key from a
    user-supplied password instead of a local key file, using a
    memory-hard KDF (Scrypt) with a persisted random salt.

    Not wired into the app yet — no UI collects a password today — but
    it satisfies the same `KeyProvider` interface as
    `LocalKeyProvider`, so switching to it later is a one-line change
    in `engine.encryption.get_default_encryption_service()` (or
    wherever the app decides to construct it), with no changes needed
    to `EncryptionService`, `engine/storage.py`, or any call site that
    reads/writes project data.
    """

    def __init__(self, password: str, salt_file: Path) -> None:
        self._password = password.encode("utf-8")
        self._salt_file = salt_file
        self._key: bytes | None = None

    def get_key(self) -> bytes:
        if self._key is not None:
            return self._key
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        self._salt_file.parent.mkdir(parents=True, exist_ok=True)
        if self._salt_file.exists():
            salt = self._salt_file.read_bytes()
        else:
            salt = os.urandom(16)
            self._salt_file.write_bytes(salt)
            try:
                os.chmod(self._salt_file, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

        kdf = Scrypt(salt=salt, length=_KEY_SIZE_BYTES, n=2**14, r=8, p=1)
        self._key = kdf.derive(self._password)
        return self._key






class EncryptionService(abc.ABC):
    """
    The only interface the rest of the app (storage.py) talks to.
    Pure bytes-in/bytes-out — no knowledge of files, projects, or JSON.
    """

    @abc.abstractmethod
    def encrypt(self, plaintext: bytes) -> bytes:
        """Encrypt plaintext bytes, returning a self-contained ciphertext blob."""
        raise NotImplementedError

    @abc.abstractmethod
    def decrypt(self, blob: bytes) -> bytes:
        """Decrypt a blob previously produced by `encrypt`."""
        raise NotImplementedError

    @abc.abstractmethod
    def is_encrypted(self, blob: bytes) -> bool:
        """
        Cheap format check (magic header only) used by the storage
        layer's migration path to tell already-encrypted files apart
        from legacy plaintext files, without attempting a decrypt.
        """
        raise NotImplementedError


class AesGcmEncryptionService(EncryptionService):
    """
    AES-256-GCM implementation, backed by the `cryptography` library's
    audited `AESGCM` primitive (not a hand-rolled cipher mode).

    A fresh random 96-bit nonce is generated for every `encrypt()` call
    (required for GCM's security guarantees — nonces must never repeat
    under the same key) and stored alongside the ciphertext so
    `decrypt()` is self-contained: callers never manage nonces
    themselves.
    """

    def __init__(self, key_provider: KeyProvider) -> None:
        self._key_provider = key_provider

    def encrypt(self, plaintext: bytes) -> bytes:
        key = self._key_provider.get_key()
        aesgcm = AESGCM(key)
        nonce = os.urandom(_NONCE_SIZE_BYTES)
        ciphertext = aesgcm.encrypt(nonce, plaintext, associated_data=None)
        return MAGIC + nonce + ciphertext

    def decrypt(self, blob: bytes) -> bytes:
        if not self.is_encrypted(blob):
            raise DecryptionError("Blob is missing the expected encryption header.")
        key = self._key_provider.get_key()
        aesgcm = AESGCM(key)
        nonce = blob[len(MAGIC):len(MAGIC) + _NONCE_SIZE_BYTES]
        ciphertext = blob[len(MAGIC) + _NONCE_SIZE_BYTES:]
        try:
            return aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        except InvalidTag as e:
            raise DecryptionError(
                "Failed to decrypt data — wrong key, or the file is corrupted/tampered with."
            ) from e

    def is_encrypted(self, blob: bytes) -> bool:
        return blob[:len(MAGIC)] == MAGIC

















_default_service: EncryptionService | None = None


def get_default_encryption_service() -> EncryptionService:
    global _default_service
    if _default_service is None:
        data_dir = Path(__file__).parent.parent / "data"
        key_provider = LocalKeyProvider(key_file=data_dir / ".encryption_key")
        _default_service = AesGcmEncryptionService(key_provider)
    return _default_service


def configure_encryption_service(service: EncryptionService) -> None:
    """
    Override the default encryption service — used by tests, and the
    intended hook for a future "unlock with password" flow to install a
    `PasswordKeyProvider`-backed service before any project I/O happens.
    """
    global _default_service
    _default_service = service
