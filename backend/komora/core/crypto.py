"""Authenticated encryption for Silpo OAuth tokens at rest."""

import base64
import binascii
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_BYTES: Final = 12  # 96 bits, the size AES-GCM is specified for
_KEY_BYTES: Final = 32  # AES-256

_HOWTO: Final = (
    "Generate one with: "
    'uv run python -c "import base64,os;'
    'print(base64.urlsafe_b64encode(os.urandom(32)).decode())"'
)


class TokenCipher:
    """AES-256-GCM over a urlsafe-base64 key.

    The nonce is random per call and prepended to the ciphertext, so encrypting the
    same token twice yields different bytes — otherwise equal tokens would be visibly
    equal in the database.

    `aad` (associated data) is authenticated but not encrypted. Binding a row to its
    owner — ``aad=f"user:{telegram_id}".encode()`` — means a ciphertext copied into
    another user's row fails to decrypt instead of silently granting their access.
    """

    __slots__ = ("_aesgcm",)

    def __init__(self, key: str) -> None:
        try:
            raw = base64.urlsafe_b64decode(key)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"KOMORA_TOKEN_ENCRYPTION_KEY is not valid urlsafe base64. {_HOWTO}"
            ) from exc

        if len(raw) != _KEY_BYTES:
            raise ValueError(
                f"KOMORA_TOKEN_ENCRYPTION_KEY must decode to {_KEY_BYTES} bytes "
                f"(AES-256), got {len(raw)}. {_HOWTO}"
            )

        self._aesgcm = AESGCM(raw)

    def encrypt(self, plaintext: str, aad: bytes | None = None) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aesgcm.encrypt(nonce, plaintext.encode(), aad)

    def decrypt(self, blob: bytes, aad: bytes | None = None) -> str:
        """Raises `cryptography.exceptions.InvalidTag` if tampered, truncated, or
        encrypted under a different key or `aad`."""
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return self._aesgcm.decrypt(nonce, ciphertext, aad).decode()
