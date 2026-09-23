"""Refresh canonical end-of-day market prices from yfinance.

The existing ADMIN-derived rows remain immutable historical lineage.  This
pipeline appends only missing daily rows to PORTFOLIO_APP.MK_DAILY_PRICE and
records its own MK_ETL_RUN audit record.  It deliberately stores the provider
``Close`` field with ``auto_adjust=False``: a column named ``close_price`` must
not silently change meaning to an adjusted close.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo

import oracledb
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from check_market_data_quality import print_report, run_market_quality_check
from portfolio_dashboard.data_quality import overall_status
from portfolio_dashboard.database import managed_connection
from portfolio_dashboard.market_data_quality import MarketQualityThresholds


YFINANCE_SOURCE = "YFINANCE_DAILY_PRICE"
DEFAULT_PILOT_LIMIT = 1
MAX_PILOT_LIMIT = 10
ALL_WATCHLIST_LIMIT = 100
DEFAULT_REQUEST_DELAY_SECONDS = 0.5
MAX_REQUEST_DELAY_SECONDS = 5.0
MARKET_CLOSE_READY_TIME = clock_time(16, 30)
NEW_YORK = ZoneInfo("America/New_York")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("market_daily_price_pipeline.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("MarketDailyPriceETL")


class DailyPriceCandidate(BaseModel):
    """A canonical security eligible for a daily-price provider request."""

    model_config = ConfigDict(str_strip_whitespace=True)

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    sector: str | None = Field(default=None, max_length=50)


class DailyPriceRecord(BaseModel):
    """A finite end-of-day price ready for immutable canonical insertion."""

    security_key: int = Field(gt=0)
    ticker: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    trade_date: date
    close_price: float = Field(gt=0)
    volume: int = Field(ge=0)

    @model_validator(mode="after")
    def require_finite_close(self) -> "DailyPriceRecord":
        if not math.isfinite(self.close_price):
            raise ValueError("close_price must be finite.")
        return self


class YFinanceProviderError(RuntimeError):
    """A provider error safe to log without leaking an external request URL."""


@dataclass(frozen=True)
class CandidateMetrics:
    """Data-quality counts from one read-only security selection."""

    selected_count: int
    validated_count: int
    rejected_count: int


@dataclass(frozen=True)
class PipelineMetrics:
    """Measured outcomes for one live daily-price refresh."""

    extracted_count: int
    validated_count: int
    rejected_count: int
    loaded_count: int


MARKET_PRICE_CANDIDATE_QUERY = """
    SELECT security_key, ticker, sector
    FROM (
        SELECT security.security_key, security.ticker, security.sector
        FROM mk_security security
        WHERE security.ticker IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM mk_daily_price price
              JOIN mk_etl_run run_audit
                  ON run_audit.run_id = price.run_id
                 AND run_audit.run_status IN ('SUCCEEDED', 'PARTIAL')
              WHERE price.security_key = security.security_key
                AND price.trade_date >= :required_through_date
          )
        ORDER BY security.ticker
    )
    WHERE ROWNUM <= :limit
"""

LAST_SUCCESSFUL_PRICE_DATE_QUERY = """
    SELECT MAX(price.trade_date)
    FROM mk_daily_price price
    JOIN mk_etl_run run_audit
        ON run_audit.run_id = price.run_id
       AND run_audit.run_status IN ('SUCCEEDED', 'PARTIAL')
    WHERE price.security_key = :security_key
"""

CANDIDATE_COLUMNS = ("security_key", "ticker", "sector")


def validate_pilot_limit(value: int) -> int:
    """Keep manual pilots deliberately small."""
    if not 1 <= value <= MAX_PILOT_LIMIT:
        raise ValueError(
            f"Pilot limit must be between 1 and {MAX_PILOT_LIMIT}; received {value}."
        )
    return value


def resolve_refresh_limit(all_watchlist: bool, requested_limit: int | None) -> int:
    """Choose either a small pilot or the fixed all-watchlist safety cap."""
    if all_watchlist:
        if requested_limit is not None:
            raise ValueError("--all-watchlist cannot be combined with --limit.")
        return ALL_WATCHLIST_LIMIT
    return validate_pilot_limit(requested_limit or DEFAULT_PILOT_LIMIT)


def validate_request_delay_seconds(value: float) -> float:
    """Keep voluntary inter-security request pacing within a safe range."""
    if not 0 <= value <= MAX_REQUEST_DELAY_SECONDS:
        raise ValueError(
            "Request delay must be between 0 and "
            f"{MAX_REQUEST_DELAY_SECONDS:g} seconds; received {value}."
        )
    return value


def provider_end_date(now: datetime | None = None) -> date:
    """Return yfinance's exclusive end date without requesting a partial bar.

    Before 4:30 p.m. New York time, end at today so yfinance can return only
    completed sessions before today.  After that buffer, end tomorrow so the
    completed current session is eligible.  The provider's ``end`` parameter is
    exclusive.
    """
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    local = instant.astimezone(NEW_YORK)
    if local.timetz().replace(tzinfo=None) >= MARKET_CLOSE_READY_TIME:
        return local.date() + timedelta(days=1)
    return local.date()


def previous_weekday(value: date) -> date:
    """Return the preceding weekday; provider holidays are handled as no-data."""
    candidate = value - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def required_through_date(now: datetime | None = None) -> date:
    """Return the latest ordinary weekday a completed-price refresh can expect."""
    return previous_weekday(provider_end_date(now))


def validate_candidate_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[list[DailyPriceCandidate], int]:
    """Validate database candidates before any yfinance request is made."""
    candidates: list[DailyPriceCandidate] = []
    rejected_count = 0
    for row in rows:
        payload = dict(zip(CANDIDATE_COLUMNS, row, strict=True))
        try:
            candidates.append(DailyPriceCandidate.model_validate(payload))
        except ValidationError as error:
            rejected_count += 1
            logger.warning(
                "Rejected daily-price candidate security_key=%s: %s",
                payload.get("security_key"),
                error.errors(),
            )
    return candidates, rejected_count


def load_price_candidates(
    cursor: oracledb.Cursor,
    limit: int,
    required_date: date,
) -> tuple[list[DailyPriceCandidate], CandidateMetrics]:
    """Read securities that have not reached the current completed session."""
    cursor.execute(
        MARKET_PRICE_CANDIDATE_QUERY,
        {"limit": limit, "required_through_date": required_date},
    )
    rows = cursor.fetchall()
    candidates, rejected_count = validate_candidate_rows(rows)
    return candidates, CandidateMetrics(
        selected_count=len(rows),
        validated_count=len(candidates),
        rejected_count=rejected_count,
    )


def last_successful_price_date(
    cursor: oracledb.Cursor,
    security_key: int,
) -> date | None:
    """Return the latest price loaded by a completed canonical run."""
    cursor.execute(LAST_SUCCESSFUL_PRICE_DATE_QUERY, {"security_key": security_key})
    row = cursor.fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError("Oracle returned an unexpected trade_date type.")


def fetch_yfinance_history(
    ticker: str,
    start_date: date,
    end_date: date,
) -> Any:
    """Fetch unadjusted daily rows without embedding provider details in errors."""
    if start_date >= end_date:
        raise ValueError("start_date must be before end_date.")
    try:
        import yfinance as yf
    except ImportError as error:
        raise RuntimeError(
            "The yfinance package is required. Install dependencies with "
            "pip install -r requirements.txt."
        ) from error

    try:
        history = yf.Ticker(ticker).history(
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=False,
            actions=False,
            repair=False,
            timeout=20,
        )
    except Exception:
        raise YFinanceProviderError(
            f"yfinance daily-price request could not be completed for ticker {ticker}."
        ) from None

    if not hasattr(history, "iterrows") or not hasattr(history, "empty"):
        raise YFinanceProviderError(
            f"yfinance returned an unexpected daily-price payload for ticker {ticker}."
        )
    return history


def _to_trade_date(value: object) -> date:
    """Normalize a pandas date-like index value to a provider session date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        converted = to_pydatetime()
        if isinstance(converted, datetime):
            return converted.date()
    raise ValueError("Provider returned an invalid daily-price index value.")


def _to_finite_number(value: object, field_name: str) -> float:
    """Convert a provider scalar to a finite number without inventing a value."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"Provider {field_name} is missing or invalid.")
    try:
        converted = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Provider {field_name} is not numeric.") from error
    if not math.isfinite(converted):
        raise ValueError(f"Provider {field_name} is not finite.")
    return converted


def validate_daily_price_records(
    candidate: DailyPriceCandidate,
    history: Any,
    end_date: date,
) -> tuple[list[DailyPriceRecord], int]:
    """Validate, de-duplicate, and bound returned yfinance daily rows."""
    valid_records: dict[date, DailyPriceRecord] = {}
    rejected_count = 0
    for index_value, row in history.iterrows():
        trade_date: date | None = None
        try:
            trade_date = _to_trade_date(index_value)
            if trade_date >= end_date:
                raise ValueError("Provider returned a non-completed trade date.")
            close_price = _to_finite_number(row.get("Close"), "Close")
            volume_value = _to_finite_number(row.get("Volume"), "Volume")
            if not volume_value.is_integer():
                raise ValueError("Provider volume is not a whole number.")
            record = DailyPriceRecord.model_validate(
                {
                    "security_key": candidate.security_key,
                    "ticker": candidate.ticker,
                    "trade_date": trade_date,
                    "close_price": close_price,
                    "volume": int(volume_value),
                }
            )
        except (ValidationError, ValueError) as error:
            rejected_count += 1
            logger.warning(
                "Rejected yfinance daily price for ticker=%s, trade_date=%s: %s",
                candidate.ticker,
                trade_date or "unknown",
                error,
            )
            continue
        valid_records[record.trade_date] = record
    return list(sorted(valid_records.values(), key=lambda item: item.trade_date)), rejected_count


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
    """Create a RUNNING audit record before any provider request occurs."""
    run_id_var = cursor.var(oracledb.DB_TYPE_NUMBER)
    cursor.execute(
        """
        INSERT INTO mk_etl_run (source_name, run_status)
        VALUES (:source_name, 'RUNNING')
        RETURNING run_id INTO :run_id
        """,
        {"source_name": YFINANCE_SOURCE, "run_id": run_id_var},
    )
    return _returned_number(run_id_var.getvalue())


def insert_daily_price(
    cursor: oracledb.Cursor,
    record: DailyPriceRecord,
    run_id: int,
) -> int:
    """Insert only a missing security/date row; never rewrite historical lineage."""
    cursor.execute(
        """
        INSERT INTO mk_daily_price (
            security_key,
            trade_date,
            run_id,
            source_name,
            close_price,
            volume
        )
        SELECT
            :security_key,
            :trade_date,
            :run_id,
            :source_name,
            :close_price,
            :volume
        FROM dual
        WHERE NOT EXISTS (
            SELECT 1
            FROM mk_daily_price existing_price
            WHERE existing_price.security_key = :security_key
              AND existing_price.trade_date = :trade_date
        )
        """,
        {
            "security_key": record.security_key,
            "trade_date": record.trade_date,
            "run_id": run_id,
            "source_name": YFINANCE_SOURCE,
            "close_price": record.close_price,
            "volume": record.volume,
        },
    )
    return int(cursor.rowcount or 0)


def complete_run(
    cursor: oracledb.Cursor,
    run_id: int,
    metrics: PipelineMetrics,
) -> None:
    """Finalize a successful or partial audit row with measured counts."""
    run_status = "PARTIAL" if metrics.rejected_count else "SUCCEEDED"
    cursor.execute(
        """
        UPDATE mk_etl_run
        SET
            completed_at = SYSTIMESTAMP,
            run_status = :run_status,
            extracted_count = :extracted_count,
            validated_count = :validated_count,
            rejected_count = :rejected_count,
            loaded_count = :loaded_count,
            error_message = NULL
        WHERE run_id = :run_id
        """,
        {
            "run_id": run_id,
            "run_status": run_status,
            "extracted_count": metrics.extracted_count,
            "validated_count": metrics.validated_count,
            "rejected_count": metrics.rejected_count,
            "loaded_count": metrics.loaded_count,
        },
    )


def fail_run(cursor: oracledb.Cursor, run_id: int, error_message: str) -> None:
    """Persist a failure without storing an unbounded external exception."""
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


def _fetch_start_date(last_trade_date: date | None, end_date: date) -> date:
    """Choose the first missing provider day, or a bounded initial backfill."""
    if last_trade_date is not None:
        return last_trade_date + timedelta(days=1)
    return end_date - timedelta(days=31)


def run_probe(ticker: str, now: datetime | None = None) -> None:
    """Make exactly one provider request without reading or writing Oracle."""
    normalized_ticker = ticker.strip().upper()
    DailyPriceCandidate.model_validate(
        {"security_key": 1, "ticker": normalized_ticker, "sector": None}
    )
    end_date = provider_end_date(now)
    history = fetch_yfinance_history(
        normalized_ticker,
        end_date - timedelta(days=10),
        end_date,
    )
    if history.empty:
        raise YFinanceProviderError(
            f"yfinance returned no recent daily prices for ticker {normalized_ticker}."
        )
    print("YFINANCE DAILY-PRICE PROBE PASSED")
    print(f"Ticker: {normalized_ticker}")
    print("Close policy: provider Close with auto_adjust=False")
    print(f"Returned daily rows: {len(history):,}")
    print("No Oracle rows were read or written.")


def run_dry_run(limit: int, now: datetime | None = None) -> CandidateMetrics:
    """Read and validate eligible securities without provider calls or writes."""
    required_date = required_through_date(now)
    logger.info(
        "Starting daily-price dry run: source=%s, limit=%s, required_through_date=%s. "
        "No yfinance calls and no Oracle writes will occur.",
        YFINANCE_SOURCE,
        limit,
        required_date,
    )
    with managed_connection() as connection:
        with connection.cursor() as cursor:
            candidates, metrics = load_price_candidates(cursor, limit, required_date)
    for candidate in candidates:
        logger.info(
            "Candidate security_key=%s | ticker=%s | sector=%s",
            candidate.security_key,
            candidate.ticker,
            candidate.sector or "Unknown",
        )
    logger.info(
        "Daily-price dry run complete: selected=%s, validated=%s, rejected=%s. "
        "No yfinance calls and no Oracle writes occurred.",
        metrics.selected_count,
        metrics.validated_count,
        metrics.rejected_count,
    )
    return metrics


def run_live_refresh(
    limit: int,
    request_delay_seconds: float,
    now: datetime | None = None,
) -> PipelineMetrics:
    """Fetch and append missing completed daily sessions for canonical securities."""
    delay = validate_request_delay_seconds(request_delay_seconds)
    end_date = provider_end_date(now)
    required_date = required_through_date(now)
    logger.info(
        "Starting live daily-price refresh: source=%s, limit=%s, required_through_date=%s, "
        "request_delay_seconds=%.2f.",
        YFINANCE_SOURCE,
        limit,
        required_date,
        delay,
    )
    with managed_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION DISABLE PARALLEL DML")
            run_id = create_run(cursor)
            connection.commit()
            try:
                candidates, candidate_metrics = load_price_candidates(
                    cursor, limit, required_date
                )
                extracted_count = 0
                validated_count = 0
                rejected_count = candidate_metrics.rejected_count
                loaded_count = 0

                if not candidates:
                    logger.info(
                        "No eligible securities require a completed daily-price refresh. "
                        "The run will complete successfully as an idempotent no-op."
                    )

                for position, candidate in enumerate(candidates):
                    last_date = last_successful_price_date(cursor, candidate.security_key)
                    start_date = _fetch_start_date(last_date, end_date)
                    if start_date >= end_date:
                        logger.info(
                            "Ticker=%s is already current through the provider window.",
                            candidate.ticker,
                        )
                        continue
                    try:
                        history = fetch_yfinance_history(
                            candidate.ticker, start_date, end_date
                        )
                    except YFinanceProviderError as error:
                        rejected_count += 1
                        logger.warning("%s", error)
                    else:
                        extracted_count += len(history)
                        records, record_rejections = validate_daily_price_records(
                            candidate, history, end_date
                        )
                        validated_count += len(records)
                        rejected_count += record_rejections
                        if not records:
                            logger.info(
                                "No completed daily-price rows returned for ticker=%s. "
                                "No values were fabricated.",
                                candidate.ticker,
                            )
                        for record in records:
                            loaded_count += insert_daily_price(cursor, record, run_id)
                    if delay and position < len(candidates) - 1:
                        time.sleep(delay)

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
                safe_message = f"Daily-price refresh failed: {type(error).__name__}."
                fail_run(cursor, run_id, safe_message)
                connection.commit()
                logger.exception("Live daily-price refresh run %s failed.", run_id)
                raise
    logger.info(
        "Live daily-price refresh run %s completed: extracted=%s, validated=%s, "
        "rejected=%s, loaded=%s.",
        run_id,
        metrics.extracted_count,
        metrics.validated_count,
        metrics.rejected_count,
        metrics.loaded_count,
    )
    return metrics


def positive_int(value: str) -> int:
    """Argparse validator for a positive whole number."""
    try:
        converted = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Value must be a whole number.") from error
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def build_parser() -> argparse.ArgumentParser:
    """Build explicit no-write, probe, pilot, and all-watchlist controls."""
    parser = argparse.ArgumentParser(
        description="Validate or append canonical end-of-day prices from yfinance."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--probe-yfinance",
        metavar="TICKER",
        help="Make one provider-only request; no Oracle connection or writes.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate canonical securities only; no provider calls or writes.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Call yfinance and append validated missing daily prices into Oracle.",
    )
    parser.add_argument(
        "--confirm-live-run",
        action="store_true",
        help="Required with --live to acknowledge provider usage and Oracle writes.",
    )
    parser.add_argument(
        "--all-watchlist",
        action="store_true",
        help=(
            "Process every security missing the latest completed session, up to the "
            "fixed 100-security safety cap. Cannot be combined with --limit."
        ),
    )
    parser.add_argument(
        "--limit",
        type=positive_int,
        default=None,
        help=(
            "Small pilot size (1-10; default: 1). Do not use with --all-watchlist."
        ),
    )
    parser.add_argument(
        "--request-delay-seconds",
        type=float,
        default=DEFAULT_REQUEST_DELAY_SECONDS,
        help=(
            "Delay between provider requests during live refreshes (0-5; "
            "default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--verify-price-quality",
        action="store_true",
        help=(
            "After a live refresh, run the read-only daily-price coverage and "
            "freshness gate using a 7-day freshness threshold."
        ),
    )
    return parser


def main() -> None:
    """Run the chosen provider probe, dry run, or confirmed live refresh."""
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.probe_yfinance:
            if args.all_watchlist or args.limit is not None or args.verify_price_quality:
                parser.error("--probe-yfinance cannot be combined with refresh options.")
            run_probe(args.probe_yfinance)
            return

        limit = resolve_refresh_limit(args.all_watchlist, args.limit)
        if args.dry_run:
            if args.verify_price_quality:
                parser.error("--verify-price-quality requires --live.")
            run_dry_run(limit)
            return

        if not args.confirm_live_run:
            parser.error("--live requires --confirm-live-run.")
        metrics = run_live_refresh(limit, args.request_delay_seconds)
        if args.verify_price_quality:
            thresholds = MarketQualityThresholds(
                min_price_history_coverage_pct=100.0,
                max_price_age_days=7.0,
            )
            summary, issues = run_market_quality_check(thresholds, "price-refresh")
            print_report(summary, issues, "price-refresh")
            if overall_status(issues) == "FAIL":
                raise SystemExit(1)
        if metrics.rejected_count:
            logger.warning(
                "Refresh completed with rejected provider rows or requests. "
                "Review the run audit before treating the batch as complete."
            )
    except SystemExit:
        raise
    except Exception:
        logger.exception("Market daily-price pipeline failed.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
