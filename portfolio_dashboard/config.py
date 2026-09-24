"""Centralized application configuration loaded from environment variables."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Local .env is for development only. Existing Codespaces or deployment
# environment variables take precedence because override=False.
load_dotenv(PROJECT_ROOT / ".env", override=False)


class ConfigurationError(RuntimeError):
    """Raised when required application configuration is missing or invalid."""


@dataclass(frozen=True)
class DatabaseConfig:
    """Database settings for either local TLS or hosted mTLS connections."""

    user: str
    password: str
    dsn: str
    current_schema: str | None = None
    wallet_tnsnames_b64: str | None = None
    wallet_pem_b64: str | None = None
    wallet_password: str | None = None

    @property
    def uses_wallet(self) -> bool:
        """Return True only when the complete hosted mTLS configuration exists."""
        return self.wallet_tnsnames_b64 is not None


@dataclass(frozen=True)
class Settings:
    database: DatabaseConfig
    rentcast_api_key: str | None
    rentcast_max_listings: int
    finnhub_api_key: str | None


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigurationError(
            f"Missing required environment variable: {name}. "
            "Add it to your local .env file or deployment secrets."
        )
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if value else None


def _current_schema() -> str | None:
    """Load an optional Oracle schema name without allowing SQL text."""
    value = _optional_env("DB_CURRENT_SCHEMA")
    if value is None:
        return None

    normalized_value = value.upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", normalized_value):
        raise ConfigurationError(
            "DB_CURRENT_SCHEMA must be a valid unquoted Oracle schema name."
        )
    return normalized_value


def _database_config() -> DatabaseConfig:
    """Load database settings and reject incomplete mTLS wallet configuration."""
    wallet_tnsnames_b64 = _optional_env("DB_WALLET_TNSNAMES_B64")
    wallet_pem_b64 = _optional_env("DB_WALLET_PEM_B64")
    wallet_password = _optional_env("DB_WALLET_PASSWORD")
    wallet_values = (
        wallet_tnsnames_b64,
        wallet_pem_b64,
        wallet_password,
    )

    if any(wallet_values) and not all(wallet_values):
        raise ConfigurationError(
            "Hosted Oracle mTLS configuration is incomplete. Set all three of "
            "DB_WALLET_TNSNAMES_B64, DB_WALLET_PEM_B64, and "
            "DB_WALLET_PASSWORD, or set none of them for the existing local "
            "wallet-less TLS connection."
        )

    return DatabaseConfig(
        user=_required_env("DB_USER"),
        password=_required_env("DB_PASSWORD"),
        dsn=_required_env("DB_DSN"),
        current_schema=_current_schema(),
        wallet_tnsnames_b64=wallet_tnsnames_b64,
        wallet_pem_b64=wallet_pem_b64,
        wallet_password=wallet_password,
    )


def _rentcast_max_listings() -> int:
    raw_value = os.getenv("RENTCAST_MAX_LISTINGS", "50")

    try:
        value = int(raw_value)
    except ValueError as error:
        raise ConfigurationError(
            "RENTCAST_MAX_LISTINGS must be a whole number."
        ) from error

    if not 1 <= value <= 5000:
        raise ConfigurationError(
            "RENTCAST_MAX_LISTINGS must be between 1 and 5000."
        )

    return value


@lru_cache
def get_settings() -> Settings:
    """Load and validate application settings once per Python process."""
    return Settings(
        database=_database_config(),
        rentcast_api_key=_optional_env("RENTCAST_API_KEY"),
        rentcast_max_listings=_rentcast_max_listings(),
        finnhub_api_key=_optional_env("FINNHUB_API_KEY"),
    )