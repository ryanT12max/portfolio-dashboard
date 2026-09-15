"""Shared Oracle database connection helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import oracledb

from portfolio_dashboard.config import DatabaseConfig, get_settings


def get_connection(
    database_config: DatabaseConfig | None = None,
) -> oracledb.Connection:
    """Create an Oracle connection using centrally validated configuration."""
    config = database_config or get_settings().database

    return oracledb.connect(
        user=config.user,
        password=config.password,
        dsn=config.dsn,
    )


@contextmanager
def managed_connection() -> Iterator[oracledb.Connection]:
    """Yield an Oracle connection and always close it afterward."""
    connection = get_connection()

    try:
        yield connection
    finally:
        connection.close()