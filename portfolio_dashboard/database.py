"""Shared Oracle database connection helpers."""

from __future__ import annotations

import base64
import binascii
import os
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import oracledb

from portfolio_dashboard.config import ConfigurationError, DatabaseConfig, get_settings


def _decode_base64_secret(name: str, value: str) -> bytes:
    """Decode a deployment-only wallet file without logging its contents."""
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError(
            f"{name} must be valid base64-encoded wallet content."
        ) from error

    if not decoded:
        raise ConfigurationError(f"{name} decoded to an empty wallet file.")

    return decoded


@lru_cache
def _materialize_wallet(
    tnsnames_b64: str,
    pem_b64: str,
) -> str:
    """Write Cloud-secret wallet files to a process-local, owner-only directory."""
    wallet_directory = Path(tempfile.mkdtemp(prefix="portfolio_dashboard_wallet_"))
    os.chmod(wallet_directory, 0o700)

    wallet_files = {
        "tnsnames.ora": _decode_base64_secret(
            "DB_WALLET_TNSNAMES_B64",
            tnsnames_b64,
        ),
        "ewallet.pem": _decode_base64_secret(
            "DB_WALLET_PEM_B64",
            pem_b64,
        ),
    }

    for filename, contents in wallet_files.items():
        wallet_file = wallet_directory / filename
        wallet_file.write_bytes(contents)
        os.chmod(wallet_file, 0o600)

    return str(wallet_directory)


def _connection_kwargs(config: DatabaseConfig) -> dict[str, str]:
    """Build thin-driver connection arguments for local TLS or hosted mTLS."""
    kwargs = {
        "user": config.user,
        "password": config.password,
        "dsn": config.dsn,
    }

    if config.uses_wallet:
        # _database_config() guarantees these values are present together.
        assert config.wallet_tnsnames_b64 is not None
        assert config.wallet_pem_b64 is not None
        assert config.wallet_password is not None

        wallet_directory = _materialize_wallet(
            config.wallet_tnsnames_b64,
            config.wallet_pem_b64,
        )
        kwargs.update(
            config_dir=wallet_directory,
            wallet_location=wallet_directory,
            wallet_password=config.wallet_password,
        )

    return kwargs


def get_connection(
    database_config: DatabaseConfig | None = None,
) -> oracledb.Connection:
    """Create an Oracle connection using centrally validated configuration."""
    config = database_config or get_settings().database
    connection = oracledb.connect(**_connection_kwargs(config))

    if config.current_schema:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {config.current_schema}")
        except Exception:
            connection.close()
            raise

    return connection


@contextmanager
def managed_connection() -> Iterator[oracledb.Connection]:
    """Yield an Oracle connection and always close it afterward."""
    connection = get_connection()

    try:
        yield connection
    finally:
        connection.close()