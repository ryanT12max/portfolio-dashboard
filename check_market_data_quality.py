"""Read-only quality and freshness gate for canonical market-watchlist data.

This command makes no API calls and writes no Oracle rows. It queries only
PORTFOLIO_APP canonical market reporting objects and exits non-zero for errors.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from typing import Any, Literal

import oracledb

from portfolio_dashboard.data_quality import QualityIssue, overall_status
from portfolio_dashboard.database import managed_connection
from portfolio_dashboard.market_data_quality import (
    MarketQualitySummary,
    MarketQualityThresholds,
    evaluate_fundamentals_refresh_quality,
    evaluate_market_quality,
    evaluate_price_refresh_quality,
    evaluate_quote_refresh_quality,
)


MARKET_QUALITY_SUMMARY_SQL = """
    SELECT
        COUNT(*) AS total_watchlist_securities,
        SUM(
            CASE
                WHEN watchlist.price_history_status = 'PRICE_AVAILABLE' THEN 1
                ELSE 0
            END
        ) AS securities_with_price_history,
        SUM(
            CASE
                WHEN watchlist.price_history_status = 'MISSING_PRICE_HISTORY' THEN 1
                ELSE 0
            END
        ) AS securities_missing_price_history,
        SUM(
            CASE
                WHEN current_quote.security_key IS NOT NULL THEN 1
                ELSE 0
            END
        ) AS securities_with_current_quote,
        SUM(
            CASE
                WHEN current_quote.security_key IS NULL THEN 1
                ELSE 0
            END
        ) AS securities_missing_current_quote,
        MIN(current_quote.observed_at) AS oldest_current_quote_observed_at,
        MAX(current_quote.observed_at) AS newest_current_quote_observed_at,
        SUM(
            CASE
                WHEN watchlist.fundamentals_status <> 'MISSING_FUNDAMENTALS' THEN 1
                ELSE 0
            END
        ) AS securities_with_fundamentals,
        SUM(
            CASE
                WHEN watchlist.fundamentals_status = 'MISSING_FUNDAMENTALS' THEN 1
                ELSE 0
            END
        ) AS securities_missing_fundamentals,
        SUM(
            CASE
                WHEN watchlist.fundamentals_status = 'AVAILABLE_AS_OF_UNKNOWN' THEN 1
                ELSE 0
            END
        ) AS fundamentals_with_unknown_as_of_date,
        MIN(watchlist.latest_trade_date) AS oldest_latest_price_date,
        MAX(watchlist.latest_trade_date) AS newest_latest_price_date
    FROM mk_security security
    LEFT JOIN vw_mk_watchlist_analysis watchlist
        ON watchlist.security_key = security.security_key
    LEFT JOIN vw_mk_current_quote current_quote
        ON current_quote.security_key = security.security_key
"""

DAILY_PRICE_DUPLICATES_SQL = """
    SELECT COUNT(*) AS duplicate_daily_price_groups
    FROM (
        SELECT security_key, trade_date
        FROM mk_daily_price
        GROUP BY security_key, trade_date
        HAVING COUNT(*) > 1
    )
"""

QualityScope = Literal[
    "full",
    "quote-refresh",
    "price-refresh",
    "fundamentals-refresh",
]


def _row_as_dict(cursor: oracledb.Cursor) -> dict[str, Any]:
    """Read exactly one Oracle result row using lowercase column names."""
    row = cursor.fetchone()
    if row is None:
        return {}
    columns = [description[0].lower() for description in cursor.description]
    return dict(zip(columns, row, strict=True))


def _as_int(value: Any) -> int:
    """Normalize Oracle numeric aggregates, including null aggregates, to int."""
    return int(value or 0)


def _as_date_or_datetime(value: Any) -> date | datetime | None:
    """Preserve an Oracle DATE or timestamp, while safely handling nulls."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value
    raise TypeError(f"Expected an Oracle DATE or timestamp, received {type(value)!r}.")


def fetch_market_quality_summary(cursor: oracledb.Cursor) -> MarketQualitySummary:
    """Read canonical market completeness, uniqueness, and freshness metrics."""
    cursor.execute(MARKET_QUALITY_SUMMARY_SQL)
    summary_row = _row_as_dict(cursor)

    cursor.execute(DAILY_PRICE_DUPLICATES_SQL)
    duplicate_row = _row_as_dict(cursor)

    return MarketQualitySummary(
        total_watchlist_securities=_as_int(summary_row["total_watchlist_securities"]),
        securities_with_price_history=_as_int(
            summary_row["securities_with_price_history"]
        ),
        securities_missing_price_history=_as_int(
            summary_row["securities_missing_price_history"]
        ),
        duplicate_daily_price_groups=_as_int(
            duplicate_row["duplicate_daily_price_groups"]
        ),
        securities_with_current_quote=_as_int(
            summary_row["securities_with_current_quote"]
        ),
        securities_missing_current_quote=_as_int(
            summary_row["securities_missing_current_quote"]
        ),
        oldest_current_quote_observed_at=_as_date_or_datetime(
            summary_row["oldest_current_quote_observed_at"]
        ),
        newest_current_quote_observed_at=_as_date_or_datetime(
            summary_row["newest_current_quote_observed_at"]
        ),
        securities_with_fundamentals=_as_int(
            summary_row["securities_with_fundamentals"]
        ),
        securities_missing_fundamentals=_as_int(
            summary_row["securities_missing_fundamentals"]
        ),
        fundamentals_with_unknown_as_of_date=_as_int(
            summary_row["fundamentals_with_unknown_as_of_date"]
        ),
        oldest_latest_price_date=_as_date_or_datetime(
            summary_row["oldest_latest_price_date"]
        ),
        newest_latest_price_date=_as_date_or_datetime(
            summary_row["newest_latest_price_date"]
        ),
    )


def run_market_quality_check(
    thresholds: MarketQualityThresholds,
    scope: QualityScope = "full",
) -> tuple[MarketQualitySummary, list[QualityIssue]]:
    """Run one read-only quality scope and return its summary plus issues."""
    if scope not in ("full", "quote-refresh", "price-refresh", "fundamentals-refresh"):
        raise ValueError(f"Unsupported market quality scope: {scope}.")

    with managed_connection() as connection:
        with connection.cursor() as cursor:
            summary = fetch_market_quality_summary(cursor)

    if scope == "quote-refresh":
        issues = evaluate_quote_refresh_quality(summary, thresholds)
    elif scope == "price-refresh":
        issues = evaluate_price_refresh_quality(summary, thresholds)
    elif scope == "fundamentals-refresh":
        issues = evaluate_fundamentals_refresh_quality(summary, thresholds)
    else:
        issues = evaluate_market_quality(summary, thresholds)
    return summary, issues


def print_report(
    summary: MarketQualitySummary,
    issues: list[QualityIssue],
    scope: QualityScope = "full",
) -> None:
    """Print a concise market-quality report suitable for logs and CI."""
    if scope == "quote-refresh":
        print("MARKET QUOTE REFRESH QUALITY REPORT")
        print("Scope: current quote coverage and observation freshness only.")
    elif scope == "price-refresh":
        print("MARKET DAILY-PRICE REFRESH QUALITY REPORT")
        print("Scope: historical daily-price coverage, uniqueness, and freshness only.")
    elif scope == "fundamentals-refresh":
        print("MARKET FUNDAMENTALS ENRICHMENT QUALITY REPORT")
        print("Scope: fundamentals coverage and as-of-date lineage only.")
    else:
        print("MARKET WATCHLIST DATA QUALITY AND FRESHNESS REPORT")
    print()
    print(f"Watchlist securities: {summary.total_watchlist_securities:,}")
    print(
        "Price-history coverage: "
        f"{summary.securities_with_price_history:,}/"
        f"{summary.total_watchlist_securities:,} "
        f"({summary.price_history_coverage_pct or 0:.2f}%)"
    )
    print(
        "Current-quote coverage: "
        f"{summary.securities_with_current_quote:,}/"
        f"{summary.total_watchlist_securities:,} "
        f"({summary.current_quote_coverage_pct or 0:.2f}%)"
    )
    print(
        "Current-quote observation range: "
        f"{summary.oldest_current_quote_observed_at or 'not available'} through "
        f"{summary.newest_current_quote_observed_at or 'not available'}"
    )
    print(
        "Fundamentals coverage: "
        f"{summary.securities_with_fundamentals:,}/"
        f"{summary.total_watchlist_securities:,} "
        f"({summary.fundamentals_coverage_pct or 0:.2f}%)"
    )
    print(
        "Fundamental snapshots with unknown as-of date: "
        f"{summary.fundamentals_with_unknown_as_of_date:,}"
    )
    print(
        "Latest-price date range: "
        f"{summary.oldest_latest_price_date or 'not available'} through "
        f"{summary.newest_latest_price_date or 'not available'}"
    )
    print(
        "Duplicate security/date daily-price groups: "
        f"{summary.duplicate_daily_price_groups:,}"
    )
    print()

    if not issues:
        if scope == "quote-refresh":
            print(
                "PASS: quote-refresh coverage and freshness checks passed. "
                "Historical daily-price and fundamentals checks were not evaluated in this scope."
            )
        elif scope == "price-refresh":
            print(
                "PASS: daily-price coverage, uniqueness, and freshness checks passed. "
                "Current-quote and fundamentals checks were not evaluated in this scope."
            )
        elif scope == "fundamentals-refresh":
            print(
                "PASS: fundamentals coverage target was met. Historical daily-price "
                "and current-quote checks were not evaluated in this scope."
            )
        else:
            print("PASS: all configured market quality checks passed.")
        return

    for issue in issues:
        print(f"{issue.severity.value}: {issue.check_name} — {issue.message}")
    print()
    print(f"Overall result: {overall_status(issues)}")


def positive_float(value: str) -> float:
    """Argparse validator for a positive, non-zero threshold."""
    converted = float(value)
    if converted <= 0:
        raise argparse.ArgumentTypeError("Value must be greater than zero.")
    return converted


def percentage(value: str) -> float:
    """Argparse validator for a percentage from zero through 100."""
    converted = float(value)
    if not 0 <= converted <= 100:
        raise argparse.ArgumentTypeError("Percentage must be between 0 and 100.")
    return converted


def build_parser() -> argparse.ArgumentParser:
    """Build explicit, adjustable quality thresholds for market data."""
    parser = argparse.ArgumentParser(
        description="Run read-only market watchlist quality checks against Oracle."
    )
    parser.add_argument(
        "--min-price-history-coverage-pct",
        type=percentage,
        default=100.0,
        help="Fail below this latest-price coverage percentage (default: 100).",
    )
    parser.add_argument(
        "--scope",
        choices=("full", "quote-refresh", "price-refresh", "fundamentals-refresh"),
        default="full",
        help=(
            "Quality gate to run: full evaluates all market data; quote-refresh evaluates "
            "current-quote coverage and freshness; price-refresh evaluates daily-price "
            "coverage, uniqueness, and freshness; fundamentals-refresh evaluates "
            "fundamentals coverage and as-of-date lineage (default: full)."
        ),
    )
    parser.add_argument(
        "--max-price-age-days",
        type=positive_float,
        default=7.0,
        help=(
            "Fail when the oldest per-security latest market price is older than "
            "this many days (default: 7)."
        ),
    )
    parser.add_argument(
        "--min-current-quote-coverage-pct",
        type=percentage,
        default=100.0,
        help="Fail below this current-quote coverage percentage (default: 100).",
    )
    parser.add_argument(
        "--max-current-quote-age-hours",
        type=positive_float,
        default=72.0,
        help="Fail when the oldest current quote is older than this many hours (default: 72).",
    )
    parser.add_argument(
        "--min-fundamentals-coverage-pct",
        type=percentage,
        default=25.0,
        help="Warn below this fundamentals coverage percentage (default: 25).",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are found, for stricter scheduled checks.",
    )
    return parser


def main() -> None:
    """Run the read-only market quality gate with automation-friendly exits."""
    args = build_parser().parse_args()
    thresholds = MarketQualityThresholds(
        min_price_history_coverage_pct=args.min_price_history_coverage_pct,
        max_price_age_days=args.max_price_age_days,
        min_current_quote_coverage_pct=args.min_current_quote_coverage_pct,
        max_current_quote_age_hours=args.max_current_quote_age_hours,
        min_fundamentals_coverage_pct=args.min_fundamentals_coverage_pct,
    )

    try:
        summary, issues = run_market_quality_check(thresholds, args.scope)
    except Exception as error:
        print(f"FAIL: unable to run market quality check: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print_report(summary, issues, args.scope)
    status = overall_status(issues)
    if status == "FAIL" or (args.fail_on_warning and issues):
        raise SystemExit(1)


if __name__ == "__main__":
    main()