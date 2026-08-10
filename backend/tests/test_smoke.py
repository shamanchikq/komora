"""Toolchain smoke tests.

Verifies the environment itself works before any feature code exists: the package
imports, every third-party SDK loads on this Python, and async tests actually run
(which proves pytest-asyncio's auto mode is configured correctly).
"""

import sys


def test_package_imports() -> None:
    import komora

    assert komora is not None


def test_python_version() -> None:
    assert sys.version_info >= (3, 14), f"expected Python 3.14+, got {sys.version_info}"


def test_third_party_sdks_import() -> None:
    """Installing is not the same as importing — compiled wheels can still fail here."""
    import aiogram
    import alembic
    import cryptography
    import fastapi
    import mcp
    import sqlalchemy
    from google import genai

    assert all(
        m is not None for m in (aiogram, alembic, cryptography, fastapi, mcp, sqlalchemy, genai)
    )


async def test_async_tests_run() -> None:
    """Fails to even be collected if asyncio_mode is misconfigured."""
    import asyncio

    await asyncio.sleep(0)
    assert True
