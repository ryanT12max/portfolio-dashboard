"""Validate or refresh a capped canonical market-watchlist pilot from Finnhub.

Dry-run mode reads and validates canonical securities only. Live mode requires
an explicit confirmation, requests recent daily candles from Finnhub, validates
every returned row, and upserts only into PORTFOLIO_APP's MK_DAILY_PRICE table.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

import oracledb
import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from portfolio_dashboard.config import ConfigurationError, get_settings
from portfolio_dashboard.database import managed_connection


FINNHUB_CANDLE_URL = "https://finnhub.io/api/v1/stock/candle"
MARKET_RUN_SOURCE = "FINNHUB_STOCK_CANDLE"
DAILY_PRICE_SOURCE = "FINNHUB_STOCK_CANDLE"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10
DEFAULT_LOOKBACK_DAYS = 14
MAX_LOOKBACK_DAYS = 31


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_research_pipeline.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("MarketResearchETL")


class MarketSecurity(BaseModel):
    """A valid canonical security eligible for a provider market-data request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    sector: str | None = Field(default=None, max_length=100)


class FinnhubCandlePayload(BaseModel):
    """The subset of a Finnhub daily-candle response required by this pipeline."""

    model_config = ConfigDict(extra="ignore")

    status: str = Field(alias="s")
    close_prices: list[float] = Field(default_factory=list, alias="c")
    timestamps: list[int] = Field(default_factory=list, alias="t")
    volumes: list[int] = Field(default_factory=list, alias="v")

    @model_validator(mode="after")
    def validate_candle_arrays(self) -> "FinnhubCandlePayload":
        """Ensure an OK response has aligned close, timestamp, and volume arrays."""
        if self.status == "ok" and not self.timestamps:
            raise ValueError("Finnhub returned status=ok without candle timestamps.")

        if self.status == "ok" and not (
            len(self.close_prices) == len(self.timestamps) == len(self.volumes)
        ):
            raise ValueError(
                "Finnhub candle arrays must have matching close, timestamp, and volume lengths."
            )
        return self


class DailyPriceRecord(BaseModel):
    """A validated daily closing price ready for canonical Oracle loading."""

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    trade_date: date
    close_price: float = Field(gt=0)
    volume: int = Field(ge=0)


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from one canonical security selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass(frozen=True)
class PipelineMetrics:
    """Audit counts collected during one live market-refresh execution."""

    extracted_count: int
    validated_count: int
    rejected_count: int
    loaded_count: int


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


def validate_lookback_days(value: int) -> int:
    """Bound the API lookback window so the pilot remains small and predictable."""
    if not 1 <= value <= MAX_LOOKBACK_DAYS:
        raise ValueError(
            "Lookback days must be between 1 and "
            f"{MAX_LOOKBACK_DAYS}; received {value}."
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
    """Read and validate pilot securities without provider calls or writes."""
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


def build_http_session() -> requests.Session:
    """Create a retrying HTTP session for transient provider failures."""
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def fetch_candle_payload(
    session: requests.Session,
    ticker: str,
    api_key: str,
    start_at: datetime,
    end_at: datetime,
) -> FinnhubCandlePayload:
    """Fetch a recent daily-candle response without logging the API key."""
    response = session.get(
        FINNHUB_CANDLE_URL,
        params={
            "symbol": ticker,
            "resolution": "D",
            "from": int(start_at.timestamp()),
            "to": int(end_at.timestamp()),
            "token": api_key,
        },
        headers={"accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"Finnhub returned non-JSON data for ticker {ticker}.") from error

    if not isinstance(payload, dict):
        raise RuntimeError(f"Finnhub returned an unexpected payload for ticker {ticker}.")

    try:
        candle_payload = FinnhubCandlePayload.model_validate(payload)
    except ValidationError as error:
        raise RuntimeError(
            f"Finnhub candle payload failed validation for ticker {ticker}: "
            f"{error.errors()}"
        ) from error

    if candle_payload.status == "no_data":
        logger.warning("Finnhub returned no daily candles for ticker %s.", ticker)
    elif candle_payload.status != "ok":
        raise RuntimeError(
            f"Finnhub returned unsupported candle status {candle_payload.status!r} "
            f"for ticker {ticker}."
        )

    return candle_payload


def validate_daily_price_records(
    security: MarketSecurity,
    candle_payload: FinnhubCandlePayload,
) -> tuple[list[DailyPriceRecord], int]:
    """Convert provider candles into validated, deduplicated daily-price records."""
    valid_records: dict[date, DailyPriceRecord] = {}
    rejected_count = 0

    for timestamp, close_price, volume in zip(
        candle_payload.timestamps,
        candle_payload.close_prices,
        candle_payload.volumes,
        strict=True,
    ):
        try:
            trade_date = datetime.fromtimestamp(timestamp, tz=timezone.utc).date()
        except (OSError, OverflowError, ValueError) as error:
            rejected_count += 1
            logger.warning(
                "Rejected Finnhub candle for ticker=%s invalid timestamp=%s: %s",
                security.ticker,
                timestamp,
                error,
            )
            continue

        payload = {
            "security_key": security.security_key,
            "ticker": security.ticker,
            "trade_date": trade_date,
            "close_price": close_price,
            "volume": volume,
        }
        try:
            record = DailyPriceRecord.model_validate(payload)
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected Finnhub candle for ticker=%s timestamp=%s: %s",
                security.ticker,
                timestamp,
                error.errors(),
            )
            continue
        valid_records[record.trade_date] = record

    return (
        list(sorted(valid_records.values(), key=lambda record: record.trade_date)),
        rejected_count,
    )


def _returned_number(value: object) -> int:
    """Normalize python-oracledb scalar or one-element DML RETURNING values."""
    if isinstance(value, list):
        if len(value) != 1:
            raise RuntimeError("Oracle did not return exactly one market ETL run ID.")
        value = value[0]
    if value is None:
        raise RuntimeError("Oracle did not return a market ETL run ID.")
    return int(value)


def create_run(cursor: oracledb.Cursor) -> int:
    """Create a RUNNING audit record and return its generated run ID."""
    run_id_var = cursor.var(oracledb.DB_TYPE_NUMBER)
    cursor.execute(
        """
        INSERT INTO mk_etl_run (source_name, run_status)
        VALUES (:source_name, 'RUNNING')
        RETURNING run_id INTO :run_id
        """,
        {"source_name": MARKET_RUN_SOURCE, "run_id": run_id_var},
    )
    return _returned_number(run_id_var.getvalue())


def upsert_daily_price(
    cursor: oracledb.Cursor,
    record: DailyPriceRecord,
    run_id: int,
) -> None:
    """Upsert one validated daily price using the canonical security/date key."""
    cursor.execute(
        """
        MERGE INTO mk_daily_price target
        USING (
            SELECT :security_key AS security_key,
                   :trade_date AS trade_date
            FROM dual
        ) source
        ON (
            target.security_key = source.security_key
            AND target.trade_date = source.trade_date
        )
        WHEN MATCHED THEN
            UPDATE SET
                target.run_id = :run_id,
                target.source_name = :source_name,
                target.close_price = :close_price,
                target.volume = :volume,
                target.observed_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (
                security_key,
                trade_date,
                run_id,
                source_name,
                close_price,
                volume
            )
            VALUES (
                :security_key,
                :trade_date,
                :run_id,
                :source_name,
                :close_price,
                :volume
            )
        """,
        {
            "security_key": record.security_key,
            "trade_date": record.trade_date,
            "run_id": run_id,
            "source_name": DAILY_PRICE_SOURCE,
            "close_price": record.close_price,
            "volume": record.volume,
        },
    )


def complete_run(
    cursor: oracledb.Cursor,
    run_id: int,
    metrics: PipelineMetrics,
) -> None:
    """Mark an audit record as successfully completed with measured counts."""
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET
            completed_at = SYSTIMESTAMP,
            run_status = 'SUCCEEDED',
            extracted_count = :extracted_count,
            validated_count = :validated_count,
            rejected_count = :rejected_count,
            loaded_count = :loaded_count,
            error_message = NULL
        WHERE run_id = :run_id
        """,
        {
            "run_id": run_id,
            "extracted_count": metrics.extracted_count,
            "validated_count": metrics.validated_count,
            "rejected_count": metrics.rejected_count,
            "loaded_count": metrics.loaded_count,
        },
    )


def fail_run(
    cursor: oracledb.Cursor,
    run_id: int,
    error_message: str,
) -> None:
    """Persist a failed audit status after the market transaction rolls back."""
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET
            completed_at = SYSTIMESTAMP,
            run_status = 'FAILED',
            error_message = SUBSTR(:error_message, 1, 1000)
        WHERE run_id = :run_id
        """,
        {"run_id": run_id, "error_message": error_message},
    )


def _require_finnhub_api_key() -> str:
    """Read the provider key without exposing it in logs or console output."""
    api_key = get_settings().finnhub_api_key
    if not api_key:
        raise ConfigurationError(
            "Missing FINNHUB_API_KEY. Add it to your local .env file or Codespaces "
            "secrets before enabling live market enrichment."
        )
    return api_key


def run_dry_run(limit: int) -> CandidateMetrics:
    """Log a validated read-only cohort and return its quality metrics."""
    _require_finnhub_api_key()
    logger.info(
        "Starting market-research dry run: source=%s, limit=%s. "
        "No Finnhub calls and no Oracle writes will occur.",
        MARKET_RUN_SOURCE,
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


def run_live_pilot(limit: int, lookback_days: int) -> PipelineMetrics:
    """Fetch and load a small, confirmed daily-candle pilot into Oracle."""
    limit = validate_limit(limit)
    lookback_days = validate_lookback_days(lookback_days)
    api_key = _require_finnhub_api_key()
    end_at = datetime.now(timezone.utc)
    start_at = end_at - timedelta(days=lookback_days)
    session = build_http_session()

    logger.info(
        "Starting live market pilot: source=%s, securities=%s, lookback_days=%s.",
        MARKET_RUN_SOURCE,
        limit,
        lookback_days,
    )

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = create_run(cursor)
            connection.commit()

            try:
                securities, candidate_metrics = load_pilot_securities(cursor, limit)
                extracted_count = 0
                validated_count = 0
                rejected_count = candidate_metrics.rejected_count
                loaded_count = 0

                for security in securities:
                    candle_payload = fetch_candle_payload(
                        session=session,
                        ticker=security.ticker,
                        api_key=api_key,
                        start_at=start_at,
                        end_at=end_at,
                    )
                    extracted_count += len(candle_payload.timestamps)
                    records, record_rejections = validate_daily_price_records(
                        security,
                        candle_payload,
                    )
                    validated_count += len(records)
                    rejected_count += record_rejections

                    for record in records:
                        upsert_daily_price(cursor, record, run_id)
                        loaded_count += 1

                if not securities:
                    raise RuntimeError("No valid canonical securities were available for the pilot.")
                if loaded_count == 0:
                    raise RuntimeError(
                        "Live market pilot received no valid daily prices; no refresh was committed."
                    )

                metrics = PipelineMetrics(
                    extracted_count=extracted_count,
                    validated_count=validated_count,
                    rejected_count=rejected_count,
                    loaded_count=loaded_count,
                )
                complete_run(cursor, run_id, metrics)
                connection.commit()
            except Exception as error:
                connection.rollback()
                fail_run(cursor, run_id, str(error))
                connection.commit()
                logger.exception("Live market pilot run %s failed.", run_id)
                raise

    logger.info(
        "Live market pilot run %s completed: extracted=%s, validated=%s, "
        "rejected=%s, loaded=%s.",
        run_id,
        metrics.extracted_count,
        metrics.validated_count,
        metrics.rejected_count,
        metrics.loaded_count,
    )
    return metrics


def positive_int(value: str) -> int:
    """Argparse validator for an integer that must be positive."""
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a whole number.") from error
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def build_parser() -> argparse.ArgumentParser:
    """Build dry-run and explicit-confirmation controls for the pilot."""
    parser = argparse.ArgumentParser(
        description="Validate or refresh a capped canonical market-watchlist pilot."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate canonical securities only; no API calls or Oracle writes.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Call Finnhub and upsert validated daily prices into Oracle.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge API usage and Oracle writes.",
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=DEFAULT_PILOT_LIMIT,
        help=f"Securities to process (1-{MAX_PILOT_LIMIT}; default: %(default)s).",
    )
    parser.add_argument(
        "--lookback-days",
        type=positive_int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=(
            f"Recent calendar days to request (1-{MAX_LOOKBACK_DAYS}; "
            "default: %(default)s)."
        ),
    )
    return parser


def main() -> None:
    """Run either the no-call gate or the explicit confirmed live pilot."""
    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.dry_run:
            run_dry_run(args.limit)
            return

        if not args.confirm_live_run:
            parser.error("--live requires --confirm-live-run.")

        run_live_pilot(args.limit, args.lookback_days)
    except Exception:
        logger.exception("Market-research pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()