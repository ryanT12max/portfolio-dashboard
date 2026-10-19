"""Read-only quality and freshness gate for the canonical real-estate model.

This command makes no RentCast API calls and writes no Oracle rows. It queries
only PORTFOLIO_APP reporting objects and exits non-zero for quality failures.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any

import oracledb

from portfolio_dashboard.data_quality import (
    PipelineRun,
    QualitySummary,
    QualityThresholds,
    Severity,
    evaluate_quality,
    overall_status,
)
from portfolio_dashboard.database import managed_connection


QUALITY_SUMMARY_SQL = """
    SELECT
        COUNT(*) AS total_current_listings,
        COUNT(DISTINCT property_key) AS distinct_current_properties,
        COUNT(*) - COUNT(DISTINCT property_key) AS duplicate_current_listing_rows,
        SUM(
            CASE
                WHEN asking_price IS NULL OR asking_price <= 0 THEN 1
                ELSE 0
            END
        ) AS missing_or_invalid_asking_price,
        SUM(
            CASE
                WHEN latitude IS NULL OR longitude IS NULL THEN 1
                ELSE 0
            END
        ) AS missing_coordinates,
        SUM(
            CASE
                WHEN bedrooms IS NULL
                  OR bathrooms IS NULL
                  OR structure_sqft IS NULL
                  OR structure_sqft <= 0 THEN 1
                ELSE 0
            END
        ) AS incomplete_core_attributes,
        SUM(
            CASE
            WHEN rent_estimate_status IN ('ESTIMATED_PROPERTY', 'ESTIMATED_UNIT') THEN 1
            ELSE 0
        END
        ) AS rent_estimate_count,
        SUM(
            CASE
                WHEN rent_estimate_status IN ('MISSING', 'RETRY_PENDING', 'UNAVAILABLE') THEN 1
            ELSE 0
        END
        ) AS rent_estimate_missing_count,
        SUM(
            CASE
                WHEN rent_estimate_status = 'ESTIMATED_PROPERTY' THEN 1
                ELSE 0
            END
        ) AS property_scope_rent_estimate_count,
        SUM(
            CASE
                WHEN rent_estimate_status = 'ESTIMATED_UNIT' THEN 1
                ELSE 0
            END
        ) AS unit_scope_rent_estimate_count,
        SUM(
            CASE
                WHEN rent_estimate_status = 'UNAVAILABLE' THEN 1
                ELSE 0
            END
        ) AS rent_estimate_unavailable_count,
        SUM(
            CASE
                WHEN rent_estimate_status = 'RETRY_PENDING' THEN 1
                ELSE 0
            END
        ) AS rent_estimate_retry_pending_count
    FROM vw_re_current_investment_analysis
    WHERE listing_status = 'Active' OR listing_status IS NULL
"""

DUPLICATE_RENT_ESTIMATE_SQL = """
    SELECT COUNT(*) AS duplicate_rent_estimate_groups
    FROM (
        SELECT property_key, run_id
        FROM re_rent_estimate_snapshot
        GROUP BY property_key, run_id
        HAVING COUNT(*) > 1
    )
"""

LATEST_RUN_SQL = """
    SELECT run_id, source_name, completed_at, run_status, loaded_count
    FROM re_etl_run
    WHERE source_name = :source_name
    ORDER BY run_id DESC
    FETCH FIRST 1 ROW ONLY
"""


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


def fetch_quality_summary(cursor: oracledb.Cursor) -> QualitySummary:
    """Read the current reporting population and completeness metrics."""
    cursor.execute(QUALITY_SUMMARY_SQL)
    summary_row = _row_as_dict(cursor)

    cursor.execute(DUPLICATE_RENT_ESTIMATE_SQL)
    duplicate_row = _row_as_dict(cursor)

    return QualitySummary(
        total_current_listings=_as_int(summary_row["total_current_listings"]),
        distinct_current_properties=_as_int(
            summary_row["distinct_current_properties"]
        ),
        duplicate_current_listing_rows=_as_int(
            summary_row["duplicate_current_listing_rows"]
        ),
        missing_or_invalid_asking_price=_as_int(
            summary_row["missing_or_invalid_asking_price"]
        ),
        missing_coordinates=_as_int(summary_row["missing_coordinates"]),
        incomplete_core_attributes=_as_int(
            summary_row["incomplete_core_attributes"]
        ),
        rent_estimate_count=_as_int(summary_row["rent_estimate_count"]),
        rent_estimate_missing_count=_as_int(
            summary_row["rent_estimate_missing_count"]
        ),
        duplicate_rent_estimate_groups=_as_int(
            duplicate_row["duplicate_rent_estimate_groups"]
        ),
        property_scope_rent_estimate_count=_as_int(
            summary_row["property_scope_rent_estimate_count"]
        ),
        unit_scope_rent_estimate_count=_as_int(
            summary_row["unit_scope_rent_estimate_count"]
        ),
        rent_estimate_unavailable_count=_as_int(
            summary_row["rent_estimate_unavailable_count"]
        ),
        rent_estimate_retry_pending_count=_as_int(
            summary_row["rent_estimate_retry_pending_count"]
        ),
    )


def fetch_latest_run(
    cursor: oracledb.Cursor,
    source_name: str,
) -> PipelineRun | None:
    """Return the latest audit row for one source, if it exists."""
    cursor.execute(LATEST_RUN_SQL, {"source_name": source_name})
    row = _row_as_dict(cursor)
    if not row:
        return None

    return PipelineRun(
        source_name=str(row["source_name"]),
        run_id=_as_int(row["run_id"]),
        run_status=str(row["run_status"]),
        completed_at=row["completed_at"],
        loaded_count=_as_int(row["loaded_count"]),
    )


def print_report(
    summary: QualitySummary,
    listing_run: PipelineRun | None,
    rent_run: PipelineRun | None,
    issues: list,
) -> None:
    """Print a concise report suitable for terminal logs and CI output."""
    print("REAL-ESTATE DATA QUALITY AND FRESHNESS REPORT")
    print()
    print(f"Current listings: {summary.total_current_listings:,}")
    print(f"Distinct properties: {summary.distinct_current_properties:,}")
    print(f"Duplicate current rows: {summary.duplicate_current_listing_rows:,}")
    print(
        "Rent-estimate coverage: "
        f"{summary.rent_estimate_count:,}/{summary.total_current_listings:,} "
        f"({summary.rent_estimate_coverage_pct or 0:.2f}%)"
    )
    print(
        "Rent-estimate scope: "
        f"{summary.property_scope_rent_estimate_count:,} whole-property, "
        f"{summary.unit_scope_rent_estimate_count:,} single-unit"
    )
    print(
        "Rent-estimate outcomes without a value: "
        f"{summary.rent_estimate_unavailable_count:,} unavailable, "
        f"{summary.rent_estimate_retry_pending_count:,} retry pending"
    )
    print(
        "Invalid asking prices: "
        f"{summary.missing_or_invalid_asking_price:,}"
    )
    print(f"Missing coordinates: {summary.missing_coordinates:,}")
    print(f"Incomplete core attributes: {summary.incomplete_core_attributes:,}")
    print(
        "Duplicate rent-estimate property/run groups: "
        f"{summary.duplicate_rent_estimate_groups:,}"
    )
    print()

    for label, run in (
        ("Latest listing ETL", listing_run),
        ("Latest rent-estimate ETL", rent_run),
    ):
        if run is None:
            print(f"{label}: no run found")
            continue
        completed = run.completed_at.isoformat() if run.completed_at else "not completed"
        print(
            f"{label}: run #{run.run_id}, {run.run_status}, "
            f"loaded={run.loaded_count or 0}, completed={completed}"
        )

    print()
    if not issues:
        print("PASS: all configured quality checks passed.")
        return

    for issue in issues:
        print(f"{issue.severity.value}: {issue.check_name} — {issue.message}")
    print()
    print(f"Overall result: {overall_status(issues)}")


def positive_float(value: str) -> float:
    """Argparse validator for positive, non-zero thresholds."""
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
    """Build safe CLI controls for explicitly documented quality thresholds."""
    parser = argparse.ArgumentParser(
        description="Run read-only quality and freshness checks against Oracle."
    )
    parser.add_argument(
        "--max-listing-age-hours",
        type=positive_float,
        default=48.0,
        help="Fail if the latest listing ETL is older than this many hours (default: 48).",
    )
    parser.add_argument(
        "--max-rent-estimate-age-days",
        type=positive_float,
        default=30.0,
        help="Warn if the latest rent-estimate ETL is older than this many days (default: 30).",
    )
    parser.add_argument(
        "--min-rent-estimate-coverage-pct",
        type=percentage,
        default=10.0,
        help="Warn below this rent-estimate coverage percentage (default: 10).",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit non-zero when warnings are found, for stricter scheduled checks.",
    )
    return parser


def main() -> None:
    """Run the read-only quality gate and set an automation-friendly exit code."""
    args = build_parser().parse_args()
    thresholds = QualityThresholds(
        max_listing_age_hours=args.max_listing_age_hours,
        max_rent_estimate_age_days=args.max_rent_estimate_age_days,
        min_rent_estimate_coverage_pct=args.min_rent_estimate_coverage_pct,
    )

    try:
        with managed_connection() as connection:
            with connection.cursor() as cursor:
                summary = fetch_quality_summary(cursor)
                listing_run = fetch_latest_run(cursor, "RENTCAST")
                rent_run = fetch_latest_run(cursor, "RENTCAST_RENT_AVM")
    except Exception as error:
        print(f"FAIL: unable to read quality data from Oracle: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    issues = evaluate_quality(
        summary,
        listing_run,
        rent_run,
        thresholds,
        now=datetime.now(timezone.utc),
    )
    print_report(summary, listing_run, rent_run, issues)

    has_errors = any(issue.severity == Severity.ERROR for issue in issues)
    has_warnings = any(issue.severity == Severity.WARNING for issue in issues)
    if has_errors or (args.fail_on_warning and has_warnings):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
