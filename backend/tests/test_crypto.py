"""Silpo OAuth tokens are encrypted at rest; this is the primitive that does it."""

import base64
import os

import pytest

from komora.core.crypto import TokenCipher

KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()
OTHER_KEY = base64.urlsafe_b64encode(os.urandom(32)).decode()


def test_roundtrip() -> None:
    cipher = TokenCipher(KEY)
    assert cipher.decrypt(cipher.encrypt('{"access_token":"a"}')) == '{"access_token":"a"}'


def test_roundtrip_preserves_non_ascii() -> None:
    """Komora is a Ukrainian app; nothing may assume latin-1."""
    secret = '{"name":"Комора","note":"дані"}'
    cipher = TokenCipher(KEY)
    assert cipher.decrypt(cipher.encrypt(secret)) == secret


def test_ciphertext_does_not_leak_plaintext() -> None:
    assert b"access_token" not in TokenCipher(KEY).encrypt('{"access_token":"a"}')


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """A fresh random nonce per call — otherwise equal tokens are visibly equal in the DB."""
    cipher = TokenCipher(KEY)
    assert cipher.encrypt("x") != cipher.encrypt("x")


def test_tampering_is_detected() -> None:
    cipher = TokenCipher(KEY)
    blob = bytearray(cipher.encrypt("x"))
    blob[-1] ^= 1
    with pytest.raises(Exception):  # noqa: B017 - any failure is acceptable, silence is not
        cipher.decrypt(bytes(blob))


def test_truncated_blob_is_rejected() -> None:
    cipher = TokenCipher(KEY)
    with pytest.raises(Exception):  # noqa: B017
        cipher.decrypt(cipher.encrypt("x")[:8])


def test_a_different_key_cannot_decrypt() -> None:
    blob = TokenCipher(KEY).encrypt("x")
    with pytest.raises(Exception):  # noqa: B017
        TokenCipher(OTHER_KEY).decrypt(blob)


class TestAssociatedData:
    """AAD binds a ciphertext to its owner, so rows cannot be swapped between users."""

    def test_roundtrip_with_matching_aad(self) -> None:
        cipher = TokenCipher(KEY)
        assert cipher.decrypt(cipher.encrypt("x", aad=b"user:1"), aad=b"user:1") == "x"

    def test_mismatched_aad_is_rejected(self) -> None:
        cipher = TokenCipher(KEY)
        blob = cipher.encrypt("x", aad=b"user:1")
        with pytest.raises(Exception):  # noqa: B017
            cipher.decrypt(blob, aad=b"user:2")


class TestKeyValidation:
    """A bad key must fail loudly at construction, not on first use in production."""

    @pytest.mark.parametrize(
        "bad_key",
        [
            "",
            "not-base64!!",
            base64.urlsafe_b64encode(os.urandom(16)).decode(),  # AES-128, we require 256
            base64.urlsafe_b64encode(os.urandom(64)).decode(),
        ],
    )
    def test_invalid_key_rejected(self, bad_key: str) -> None:
        with pytest.raises(ValueError, match="KOMORA_TOKEN_ENCRYPTION_KEY"):
            TokenCipher(bad_key)

    def test_error_tells_you_how_to_generate_one(self) -> None:
        with pytest.raises(ValueError, match="urlsafe_b64encode"):
            TokenCipher("nope")
