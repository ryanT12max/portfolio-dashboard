"""Unit tests for market-fundamentals contracts and provider mapping."""

from __future__ import annotations

import pytest

from etl_market_fundamentals import (
    FINNHUB_SOURCE,
    FundamentalCandidate,
    FundamentalSnapshotRecord,
    ProviderFundamentalError,
    _decimal_to_pct,
    build_finnhub_record,
    create_run,
    insert_fundamental_snapshot,
    missing_finnhub_metrics,
    resolve_refresh_limit,
    validate_candidate_rows,
)


def valid_candidate() -> FundamentalCandidate:
    return FundamentalCandidate(
        security_key=1,
        ticker="AAPL",
        sector="Technology",
    )


def test_validate_candidate_rows_accepts_a_canonical_security() -> None:
    candidates, rejected_count = validate_candidate_rows([(1, "AAPL", "Technology")])

    assert rejected_count == 0
    assert candidates == [valid_candidate()]


def test_validate_candidate_rows_rejects_a_lowercase_ticker() -> None:
    candidates, rejected_count = validate_candidate_rows([(1, "aapl", "Technology")])

    assert candidates == []
    assert rejected_count == 1


def test_finnhub_metric_mapping_keeps_available_metrics_and_lineage() -> None:
    payload = {
        "metric": {
            "peBasicExclExtraTTM": 31.5,
            "pegTTM": 2.1,
            "pbQuarterly": 44.2,
            "epsBasicExclExtraItemsTTM": 7.23,
            "roeTTM": 152.8,
            "netProfitMarginTTM": 26.4,
            "totalDebt/totalEquityQuarterly": 1.58,
        },
        "date": "2026-09-23",
    }

    record = build_finnhub_record(valid_candidate(), payload)

    assert record.source_name == FINNHUB_SOURCE
    assert str(record.source_as_of_date) == "2026-09-23"
    assert record.pe_ratio == 31.5
    assert record.return_on_equity_pct == 152.8
    assert record.free_cash_flow_value is None
    assert len(record.source_payload_hash) == 64
    assert missing_finnhub_metrics(record) == []


def test_finnhub_mapping_rejects_a_payload_with_no_usable_metrics() -> None:
    with pytest.raises(ProviderFundamentalError, match="no usable fundamentals"):
        build_finnhub_record(valid_candidate(), {"metric": {}, "date": "2026-09-23"})


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.245, 24.5), (-0.1, -10.0), (142.0, 142.0), (None, None)],
)
def test_decimal_to_pct_handles_yfinance_decimal_and_percent_values(
    value: float | None,
    expected: float | None,
) -> None:
    assert _decimal_to_pct(value) == expected


def test_all_watchlist_mode_rejects_an_accidental_manual_limit() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        resolve_refresh_limit(True, 10)


def test_all_watchlist_mode_uses_the_fixed_safety_cap() -> None:
    assert resolve_refresh_limit(True, None) == 100


class RecordingCursor:
    """Minimal cursor double for verifying the snapshot insert bind payload."""

    def __init__(self) -> None:
        self.input_sizes: dict[str, object] | None = None
        self.statement: str | None = None
        self.bind_values: dict[str, object] | None = None

    def setinputsizes(self, **kwargs: object) -> None:
        self.input_sizes = kwargs

    def execute(self, statement: str, bind_values: dict[str, object]) -> None:
        self.statement = statement
        self.bind_values = bind_values


class ReturnedNumber:
    """Minimal python-oracledb variable double for a RETURNING clause."""

    def getvalue(self) -> list[int]:
        return [77]


class RunAuditCursor:
    """Minimal cursor double for verifying run creation has no record dependency."""

    def __init__(self) -> None:
        self.statement: str | None = None
        self.bind_values: dict[str, object] | None = None

    def var(self, _db_type: object) -> ReturnedNumber:
        return ReturnedNumber()

    def execute(self, statement: str, bind_values: dict[str, object]) -> None:
        self.statement = statement
        self.bind_values = bind_values


def test_create_run_binds_only_the_run_audit_values() -> None:
    cursor = RunAuditCursor()

    run_id = create_run(cursor)  # type: ignore[arg-type]

    assert run_id == 77
    assert cursor.bind_values is not None
    assert cursor.bind_values["source_name"] == "FUNDAMENTALS_ENRICHMENT"
    assert "record" not in cursor.bind_values


def test_snapshot_insert_binds_the_record_and_run_id() -> None:
    record = FundamentalSnapshotRecord(
        security_key=1,
        ticker="AAPL",
        source_name=FINNHUB_SOURCE,
        pe_ratio=31.5,
        source_payload_hash="a" * 64,
    )
    cursor = RecordingCursor()

    insert_fundamental_snapshot(cursor, record, run_id=77)  # type: ignore[arg-type]

    assert cursor.input_sizes is not None
    assert cursor.bind_values is not None
    assert cursor.bind_values["security_key"] == 1
    assert cursor.bind_values["run_id"] == 77
    assert cursor.bind_values["pe_ratio"] == 31.5
    assert "ticker" not in cursor.bind_values