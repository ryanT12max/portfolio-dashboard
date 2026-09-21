-- 007_rebackfill_market_prices_losslessly.sql
--
-- Purpose:
--   Replace the initial canonical market backfill after migration 006 widens
--   MK_DAILY_PRICE.CLOSE_PRICE. The original backfill rounded values beyond
--   six decimal places; this reload preserves source NUMBER precision.
--
-- Prerequisite:
--   While connected as ADMIN, the temporary SELECT grants from migration 005
--   must still exist for MARKET_DATA and STOCK_HISTORY.
--
-- Safety:
--   This script changes only PORTFOLIO_APP canonical snapshot rows. It leaves
--   ADMIN.MARKET_DATA and ADMIN.STOCK_HISTORY unchanged. The delete and reload
--   occur in one transaction: a failure rolls back to the original canonical
--   rows and marks the new reload audit run as FAILED.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions,
-- after migration 006.

DECLARE
    v_original_run_id          NUMBER;
    v_rebuild_run_id           NUMBER;
    v_original_run_count       NUMBER;
    v_expected_fundamental_rows NUMBER;
    v_expected_price_rows       NUMBER;
    v_existing_fundamental_rows NUMBER;
    v_existing_price_rows       NUMBER;
    v_loaded_fundamental_rows   NUMBER := 0;
    v_loaded_price_rows         NUMBER := 0;
    v_error_message             VARCHAR2(1000);
BEGIN
    SELECT COUNT(*)
    INTO v_original_run_count
    FROM mk_etl_run
    WHERE source_name = 'LEGACY_ADMIN_BACKFILL'
      AND run_status = 'SUCCEEDED';

    IF v_original_run_count != 1 THEN
        RAISE_APPLICATION_ERROR(
            -20011,
            'Precision reload stopped: exactly one successful LEGACY_ADMIN_BACKFILL run is required.'
        );
    END IF;

    SELECT run_id
    INTO v_original_run_id
    FROM mk_etl_run
    WHERE source_name = 'LEGACY_ADMIN_BACKFILL'
      AND run_status = 'SUCCEEDED';

    SELECT COUNT(*)
    INTO v_expected_fundamental_rows
    FROM admin.market_data;

    SELECT COUNT(*)
    INTO v_expected_price_rows
    FROM admin.stock_history;

    SELECT COUNT(*)
    INTO v_existing_fundamental_rows
    FROM mk_fundamental_snapshot
    WHERE run_id = v_original_run_id;

    SELECT COUNT(*)
    INTO v_existing_price_rows
    FROM mk_daily_price
    WHERE run_id = v_original_run_id;

    IF v_existing_fundamental_rows != v_expected_fundamental_rows
       OR v_existing_price_rows != v_expected_price_rows THEN
        RAISE_APPLICATION_ERROR(
            -20012,
            'Precision reload stopped: original canonical row counts do not match the legacy source.'
        );
    END IF;

    INSERT INTO mk_etl_run (
        source_name,
        run_status
    )
    VALUES (
        'LEGACY_ADMIN_PRECISION_RELOAD',
        'RUNNING'
    )
    RETURNING run_id INTO v_rebuild_run_id;

    -- Commit the audit start before changing canonical snapshots.
    COMMIT;

    DELETE FROM mk_fundamental_snapshot
    WHERE run_id = v_original_run_id;

    DELETE FROM mk_daily_price
    WHERE run_id = v_original_run_id;

    INSERT INTO mk_fundamental_snapshot (
        security_key,
        run_id,
        source_name,
        market_price,
        pe_ratio,
        peg_ratio,
        pb_ratio,
        earnings_per_share,
        return_on_equity_pct,
        profit_margin_pct,
        debt_to_equity_ratio,
        free_cash_flow_value,
        free_cash_flow_unit
    )
    SELECT
        security.security_key,
        v_rebuild_run_id,
        'LEGACY_ADMIN_MARKET_DATA',
        legacy.current_price,
        legacy.pe_ratio,
        legacy.peg_ratio,
        legacy.pb_ratio,
        legacy.eps,
        legacy.roe,
        legacy.profit_margin,
        legacy.de_ratio,
        legacy.fcf,
        'UNKNOWN_LEGACY_UNIT'
    FROM admin.market_data legacy
    JOIN mk_security security
        ON security.ticker = UPPER(TRIM(legacy.ticker));

    v_loaded_fundamental_rows := SQL%ROWCOUNT;

    INSERT INTO mk_daily_price (
        security_key,
        trade_date,
        run_id,
        source_name,
        close_price,
        volume
    )
    SELECT
        security.security_key,
        legacy.trade_date,
        v_rebuild_run_id,
        'LEGACY_ADMIN_STOCK_HISTORY',
        legacy.close_price,
        legacy.volume
    FROM admin.stock_history legacy
    JOIN mk_security security
        ON security.ticker = UPPER(TRIM(legacy.ticker));

    v_loaded_price_rows := SQL%ROWCOUNT;

    IF v_loaded_fundamental_rows != v_expected_fundamental_rows THEN
        RAISE_APPLICATION_ERROR(
            -20013,
            'Fundamental row-count mismatch during precision reload.'
        );
    END IF;

    IF v_loaded_price_rows != v_expected_price_rows THEN
        RAISE_APPLICATION_ERROR(
            -20014,
            'Daily-price row-count mismatch during precision reload.'
        );
    END IF;

    UPDATE mk_etl_run
    SET
        completed_at = SYSTIMESTAMP,
        run_status = 'FAILED',
        extracted_count = v_expected_fundamental_rows + v_expected_price_rows,
        validated_count = v_expected_fundamental_rows + v_expected_price_rows,
        rejected_count = 0,
        loaded_count = 0,
        error_message = 'Superseded after post-load reconciliation found CLOSE_PRICE precision loss.'
    WHERE run_id = v_original_run_id;

    UPDATE mk_etl_run
    SET
        completed_at = SYSTIMESTAMP,
        run_status = 'SUCCEEDED',
        extracted_count = v_expected_fundamental_rows + v_expected_price_rows,
        validated_count = v_loaded_fundamental_rows + v_loaded_price_rows,
        rejected_count = 0,
        loaded_count = v_loaded_fundamental_rows + v_loaded_price_rows
    WHERE run_id = v_rebuild_run_id;

    COMMIT;
EXCEPTION
    WHEN OTHERS THEN
        v_error_message := SUBSTR(SQLERRM, 1, 1000);
        ROLLBACK;

        IF v_rebuild_run_id IS NOT NULL THEN
            UPDATE mk_etl_run
            SET
                completed_at = SYSTIMESTAMP,
                run_status = 'FAILED',
                extracted_count = NVL(v_expected_fundamental_rows, 0)
                    + NVL(v_expected_price_rows, 0),
                validated_count = v_loaded_fundamental_rows + v_loaded_price_rows,
                rejected_count = 0,
                loaded_count = 0,
                error_message = v_error_message
            WHERE run_id = v_rebuild_run_id;

            COMMIT;
        END IF;

        RAISE;
END;
/