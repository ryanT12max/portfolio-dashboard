"""Safely select and validate canonical securities for a Finnhub market pilot.

This command is intentionally dry-run only. It reads from PORTFOLIO_APP's
MK_SECURITY table, makes no Finnhub requests, and writes no Oracle rows.
It is the safety gate before enabling a capped live market-refresh pipeline.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from typing import Iterable, Sequence

import oracledb
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.database import managed_connection


DEFAULT_PILOT_LIMIT = 3
MAX_PILOT_LIMIT = 10
SOURCE_NAME = "FINNHUB_MARKET_RESEARCH"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_research_dry_run.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("MarketResearchDryRun")


class MarketSecurity(BaseModel):
    """A valid canonical security eligible for a provider market-data request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    sector: str | None = Field(default=None, max_length=100)


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from one read-only security selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


MARKET_CANDIDATE_QUERY = """
    SELECT
        security_key,
        ticker,
        sector
    FROM (
        SELECT
            security_key,
            UPPER(TRIM(ticker)) AS ticker,
            sector
        FROM mk_security
        ORDER BY ticker
    )
    WHERE ROWNUM <= :limit
"""

CANDIDATE_COLUMNS = (
    "security_key",
    "ticker",
    "sector",
)


def validate_limit(value: int) -> int:
    """Reject accidental requests above the small, approved pilot cap."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def validate_security_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[MarketSecurity], int]:
    """Apply the provider-eligibility contract and return valid securities."""
    valid_securities: list[MarketSecurity] = []
    rejected_count = 0

    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))
        try:
            security = MarketSecurity.model_validate(payload)
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected canonical security_key=%s: %s",
                payload.get("security_key"),
                error.errors(),
            )
            continue
        valid_securities.append(security)

    return valid_securities, rejected_count


def load_pilot_securities(
    cursor: oracledb.Cursor,
    limit: int,
) -> tuple[list[MarketSecurity], CandidateMetrics]:
    """Read and validate pilot securities without API calls or database writes."""
    limit = validate_limit(limit)
    cursor.execute(MARKET_CANDIDATE_QUERY, {"limit": limit})
    rows = cursor.fetchall()
    securities, rejected_count = validate_security_rows(rows)
    metrics = CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(securities),
        rejected_count=rejected_count,
    )
    return securities, metrics


def run_dry_run(limit: int) -> CandidateMetrics:
    """Log a validated read-only cohort and return its quality metrics."""
    settings = get_settings()
    if not settings.finnhub_api_key:
        raise ConfigurationError(
            "Missing FINNHUB_API_KEY. Add it to your local .env file or Codespaces "
            "secrets before enabling live market enrichment."
        )

    logger.info(
        "Starting market-research dry run: source=%s, limit=%s. "
        "No Finnhub calls and no Oracle writes will occur.",
        SOURCE_NAME,
        limit,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            securities, metrics = load_pilot_securities(cursor, limit)

    for security in securities:
        logger.info(
            "Candidate security_key=%s | ticker=%s | sector=%s",
            security.security_key,
            security.ticker,
            security.sector or "unclassified",
        )

    logger.info(
        "Dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No Finnhub calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def build_parser() -> argparse.ArgumentParser:
    """Build the explicit safety controls for the pre-API pilot phase."""
    parser = argparse.ArgumentParser(
        description="Validate a capped canonical market-watchlist pilot without API calls or writes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required safety switch; this version does not support live enrichment.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_PILOT_LIMIT,
        help=f"Number of securities to validate (1-{MAX_PILOT_LIMIT}; default: %(default)s).",
    )
    return parser


def main() -> None:
    """Run the read-only market-pilot gate."""
    args = build_parser().parse_args()

    try:
        run_dry_run(args.limit)
    except Exception:
        logger.exception("Market-research dry run failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()