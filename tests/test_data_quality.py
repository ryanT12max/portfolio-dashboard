from datetime import datetime, timedelta, timezone

from portfolio_dashboard.data_quality import (
    PipelineRun,
    QualitySummary,
    QualityThresholds,
    Severity,
    evaluate_quality,
    overall_status,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
THRESHOLDS = QualityThresholds(
    max_listing_age_hours=48,
    max_rent_estimate_age_days=30,
    min_rent_estimate_coverage_pct=10,
)


def healthy_summary() -> QualitySummary:
    return QualitySummary(
        total_current_listings=135,
        distinct_current_properties=135,
        duplicate_current_listing_rows=0,
        missing_or_invalid_asking_price=0,
        missing_coordinates=0,
        incomplete_core_attributes=0,
        rent_estimate_count=19,
        rent_estimate_missing_count=116,
        duplicate_rent_estimate_groups=0,
    )


def successful_run(source_name: str, completed_at: datetime) -> PipelineRun:
    return PipelineRun(
        source_name=source_name,
        run_id=1,
        run_status="SUCCEEDED",
        completed_at=completed_at,
        loaded_count=50,
    )


def test_healthy_data_passes_without_issues() -> None:
    issues = evaluate_quality(
        healthy_summary(),
        successful_run("RENTCAST", NOW - timedelta(hours=2)),
        successful_run("RENTCAST_RENT_AVM", NOW - timedelta(days=1)),
        THRESHOLDS,
        now=NOW,
    )

    assert issues == []
    assert overall_status(issues) == "PASS"


def test_stale_listing_data_fails_the_quality_gate() -> None:
    issues = evaluate_quality(
        healthy_summary(),
        successful_run("RENTCAST", NOW - timedelta(hours=49)),
        successful_run("RENTCAST_RENT_AVM", NOW - timedelta(days=1)),
        THRESHOLDS,
        now=NOW,
    )

    assert any(
        issue.check_name == "listing_freshness"
        and issue.severity == Severity.ERROR
        for issue in issues
    )
    assert overall_status(issues) == "FAIL"


def test_low_rent_coverage_is_a_warning_not_a_data_failure() -> None:
    summary = QualitySummary(
        total_current_listings=135,
        distinct_current_properties=135,
        duplicate_current_listing_rows=0,
        missing_or_invalid_asking_price=0,
        missing_coordinates=0,
        incomplete_core_attributes=0,
        rent_estimate_count=9,
        rent_estimate_missing_count=126,
        duplicate_rent_estimate_groups=0,
    )

    issues = evaluate_quality(
        summary,
        successful_run("RENTCAST", NOW - timedelta(hours=2)),
        successful_run("RENTCAST_RENT_AVM", NOW - timedelta(days=1)),
        THRESHOLDS,
        now=NOW,
    )

    assert any(
        issue.check_name == "rent_estimate_coverage"
        and issue.severity == Severity.WARNING
        for issue in issues
    )
    assert overall_status(issues) == "PASS_WITH_WARNINGS"


def test_duplicate_current_rows_fail_the_quality_gate() -> None:
    summary = QualitySummary(
        total_current_listings=136,
        distinct_current_properties=135,
        duplicate_current_listing_rows=1,
        missing_or_invalid_asking_price=0,
        missing_coordinates=0,
        incomplete_core_attributes=0,
        rent_estimate_count=19,
        rent_estimate_missing_count=117,
        duplicate_rent_estimate_groups=0,
    )

    issues = evaluate_quality(
        summary,
        successful_run("RENTCAST", NOW - timedelta(hours=2)),
        successful_run("RENTCAST_RENT_AVM", NOW - timedelta(days=1)),
        THRESHOLDS,
        now=NOW,
    )

    assert any(
        issue.check_name == "current_listing_duplicates"
        and issue.severity == Severity.ERROR
        for issue in issues
    )