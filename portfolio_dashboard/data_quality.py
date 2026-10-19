"""Pure quality and freshness rules for canonical real-estate reporting data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Severity(str, Enum):
    """Severity used by the quality gate."""

    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True)
class QualityIssue:
    """One observable data-quality or freshness result."""

    severity: Severity
    check_name: str
    message: str


@dataclass(frozen=True)
class QualitySummary:
    """Aggregated facts read from the canonical reporting model."""

    total_current_listings: int
    distinct_current_properties: int
    duplicate_current_listing_rows: int
    missing_or_invalid_asking_price: int
    missing_coordinates: int
    incomplete_core_attributes: int
    rent_estimate_count: int
    rent_estimate_missing_count: int
    duplicate_rent_estimate_groups: int
    property_scope_rent_estimate_count: int = 0
    unit_scope_rent_estimate_count: int = 0
    rent_estimate_unavailable_count: int = 0
    rent_estimate_retry_pending_count: int = 0

    @property
    def rent_estimate_coverage_pct(self) -> float | None:
        """Return coverage only when a current listing population exists."""
        if self.total_current_listings == 0:
            return None
        return self.rent_estimate_count / self.total_current_listings * 100


@dataclass(frozen=True)
class PipelineRun:
    """Latest audit data for one source pipeline."""

    source_name: str
    run_id: int
    run_status: str
    completed_at: datetime | None
    loaded_count: int | None


@dataclass(frozen=True)
class QualityThresholds:
    """Explicit, adjustable expectations for freshness and coverage."""

    max_listing_age_hours: float = 48.0
    max_rent_estimate_age_days: float = 30.0
    min_rent_estimate_coverage_pct: float = 10.0


def _as_utc(timestamp: datetime) -> datetime:
    """Treat Oracle timestamps without a timezone as UTC for age calculations."""
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def age_in_hours(timestamp: datetime | None, now: datetime) -> float | None:
    """Return the elapsed age in hours, or None when no completion exists."""
    if timestamp is None:
        return None
    return (_as_utc(now) - _as_utc(timestamp)).total_seconds() / 3600


def evaluate_quality(
    summary: QualitySummary,
    listing_run: PipelineRun | None,
    rent_run: PipelineRun | None,
    thresholds: QualityThresholds,
    now: datetime | None = None,
) -> list[QualityIssue]:
    """Evaluate quality without database access so the rules are unit-testable."""
    now = now or datetime.now(timezone.utc)
    issues: list[QualityIssue] = []

    if summary.total_current_listings == 0:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "current_listing_population",
                "No current active listings were found in the reporting view.",
            )
        )

    if (
        summary.duplicate_current_listing_rows > 0
        or summary.distinct_current_properties != summary.total_current_listings
    ):
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "current_listing_duplicates",
                "Current reporting rows are not one row per property: "
                f"{summary.duplicate_current_listing_rows} duplicate row(s) found.",
            )
        )

    if summary.missing_or_invalid_asking_price > 0:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "asking_price_validity",
                f"{summary.missing_or_invalid_asking_price} current listing(s) have "
                "a missing or non-positive asking price.",
            )
        )

    if summary.duplicate_rent_estimate_groups > 0:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "rent_estimate_duplicates",
                f"{summary.duplicate_rent_estimate_groups} duplicate property/run "
                "rent-estimate group(s) were found.",
            )
        )

    if summary.missing_coordinates > 0:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "coordinate_coverage",
                f"{summary.missing_coordinates} current listing(s) are missing "
                "latitude or longitude and cannot be mapped.",
            )
        )

    if summary.incomplete_core_attributes > 0:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "core_attribute_coverage",
                f"{summary.incomplete_core_attributes} current listing(s) are "
                "missing bedrooms, bathrooms, or valid square footage.",
            )
        )

    coverage = summary.rent_estimate_coverage_pct
    if coverage is not None and coverage < thresholds.min_rent_estimate_coverage_pct:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "rent_estimate_coverage",
                f"Rent-estimate coverage is {coverage:.2f}%, below the configured "
                f"{thresholds.min_rent_estimate_coverage_pct:.2f}% baseline.",
            )
        )

    _evaluate_listing_freshness(issues, listing_run, thresholds, now)
    _evaluate_rent_freshness(issues, rent_run, thresholds, now)
    return issues


def _evaluate_listing_freshness(
    issues: list[QualityIssue],
    listing_run: PipelineRun | None,
    thresholds: QualityThresholds,
    now: datetime,
) -> None:
    """Treat listing pipeline failures or stale market data as errors."""
    if listing_run is None:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "listing_pipeline_run",
                "No listing ETL audit record was found for source RENTCAST.",
            )
        )
        return

    if listing_run.run_status.upper() != "SUCCEEDED":
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "listing_pipeline_status",
                f"Latest listing ETL run #{listing_run.run_id} has status "
                f"{listing_run.run_status}, not SUCCEEDED.",
            )
        )

    age_hours = age_in_hours(listing_run.completed_at, now)
    if age_hours is None:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "listing_freshness",
                "Latest listing ETL run has no completion timestamp.",
            )
        )
    elif age_hours > thresholds.max_listing_age_hours:
        issues.append(
            QualityIssue(
                Severity.ERROR,
                "listing_freshness",
                f"Latest listing ETL data is {age_hours:.1f} hours old, above the "
                f"{thresholds.max_listing_age_hours:.1f}-hour threshold.",
            )
        )


def _evaluate_rent_freshness(
    issues: list[QualityIssue],
    rent_run: PipelineRun | None,
    thresholds: QualityThresholds,
    now: datetime,
) -> None:
    """Surface rent-estimate issues as warnings because enrichment is partial."""
    if rent_run is None:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "rent_estimate_pipeline_run",
                "No rent-estimate ETL audit record was found yet.",
            )
        )
        return

    if rent_run.run_status.upper() != "SUCCEEDED":
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "rent_estimate_pipeline_status",
                f"Latest rent-estimate ETL run #{rent_run.run_id} has status "
                f"{rent_run.run_status}, not SUCCEEDED.",
            )
        )

    age_hours = age_in_hours(rent_run.completed_at, now)
    if age_hours is None:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "rent_estimate_freshness",
                "Latest rent-estimate ETL run has no completion timestamp.",
            )
        )
    elif age_hours > thresholds.max_rent_estimate_age_days * 24:
        issues.append(
            QualityIssue(
                Severity.WARNING,
                "rent_estimate_freshness",
                f"Latest rent estimates are {age_hours / 24:.1f} days old, above "
                f"the {thresholds.max_rent_estimate_age_days:.1f}-day threshold.",
            )
        )


def overall_status(issues: list[QualityIssue]) -> str:
    """Return the highest-level report outcome."""
    if any(issue.severity == Severity.ERROR for issue in issues):
        return "FAIL"
    if issues:
        return "PASS_WITH_WARNINGS"
    return "PASS"