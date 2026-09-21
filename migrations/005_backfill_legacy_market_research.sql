-- 005_backfill_legacy_market_research.sql
--
-- Purpose:
--   Copy legacy ADMIN market-research data into the PORTFOLIO_APP canonical
--   market schema created by migration 004.
--
-- Prerequisite:
--   While connected as ADMIN, run:
--     GRANT SELECT ON market_data TO portfolio_app;
--     GRANT SELECT ON stock_history TO portfolio_app;
--
-- Safety:
--   This script reads ADMIN.MARKET_DATA and ADMIN.STOCK_HISTORY only.
--   It never alters or deletes legacy ADMIN data. It refuses to run when the
--   canonical fundamentals or daily-price tables already contain data.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions.

DECLARE
    v_run_id                    NUMBER;
    v_expected_fundamental_rows NUMBER;
    v_expected_price_rows       NUMBER;
    v_loaded_fundamental_rows   NUMBER := 0;
    v_loaded_price_rows         NUMBER := 0;
    v_existing_fundamental_rows NUMBER;
    v_existing_price_rows       NUMBER;
BEGIN
    SELECT COUNT(*)
    INTO v_existing_fundamental_rows
    FROM mk_fundamental_snapshot;

    SELECT COUNT(*)
    INTO v_existing_price_rows
    FROM mk_daily_price;

    IF v_existing_fundamental_rows > 0 OR v_existing_price_rows > 0 THEN
        RAISE_APPLICATION_ERROR(
            -20001,
            'Legacy market backfill stopped: canonical market tables already contain data.'
        );
    END IF;

    SELECT COUNT(*)
    INTO v_expected_fundamental_rows
    FROM admin.market_data;

    SELECT COUNT(*)
    INTO v_expected_price_rows
    FROM admin.stock_history;

    INSERT INTO mk_etl_run (
        source_name,
        run_status
    )
    VALUES (
        'LEGACY_ADMIN_BACKFILL',
        'RUNNING'
    )
    RETURNING run_id INTO v_run_id;

    -- Commit the audit start before the copy so a later failure remains visible.
    COMMIT;

    MERGE INTO mk_security target
    USING (
        SELECT
            ticker,
            MAX(sector) AS sector
        FROM (
            SELECT
                UPPER(TRIM(ticker)) AS ticker,
                sector
            FROM admin.market_data

            UNION ALL

            SELECT
                UPPER(TRIM(ticker)) AS ticker,
                CAST(NULL AS VARCHAR2(50)) AS sector
            FROM admin.stock_history
        )
        GROUP BY ticker
    ) source
    ON (target.ticker = source.ticker)
    WHEN MATCHED THEN
        UPDATE SET
            target.sector = COALESCE(source.sector, target.sector),
            target.updated_at = SYSTIMESTAMP
    WHEN NOT MATCHED THEN
        INSERT (
            ticker,
            sector
        )
        VALUES (
            source.ticker,
            source.sector
        );

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
        v_run_id,
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
        v_run_id,
        'LEGACY_ADMIN_STOCK_HISTORY',
        legacy.close_price,
        legacy.volume
    FROM admin.stock_history legacy
    JOIN mk_security security
        ON security.ticker = UPPER(TRIM(legacy.ticker));

    v_loaded_price_rows := SQL%ROWCOUNT;

    IF v_loaded_fundamental_rows != v_expected_fundamental_rows THEN
        RAISE_APPLICATION_ERROR(
            -20002,
            'Fundamental row-count mismatch during legacy market backfill.'
        );
    END IF;

    IF v_loaded_price_rows != v_expected_price_rows THEN
        RAISE_APPLICATION_ERROR(
            -20003,
            'Daily-price row-count mismatch during legacy market backfill.'
        );
    END IF;

    UPDATE mk_etl_run
    SET
        completed_at = SYSTIMESTAMP,
        run_status = 'SUCCEEDED',
        extracted_count = v_expected_fundamental_rows + v_expected_price_rows,
        validated_count = v_loaded_fundamental_rows + v_loaded_price_rows,
        rejected_count = 0,
        loaded_count = v_loaded_fundamental_rows + v_loaded_price_rows
    WHERE run_id = v_run_id;

    COMMIT;
EXCEPTION
    WHEN OTHERS THEN
        ROLLBACK;

        IF v_run_id IS NOT NULL THEN
            UPDATE mk_etl_run
            SET
                completed_at = SYSTIMESTAMP,
                run_status = 'FAILED',
                extracted_count = NVL(v_expected_fundamental_rows, 0)
                    + NVL(v_expected_price_rows, 0),
                validated_count = v_loaded_fundamental_rows + v_loaded_price_rows,
                rejected_count = 0,
                loaded_count = 0,
                error_message = SUBSTR(SQLERRM, 1, 1000)
            WHERE run_id = v_run_id;

            COMMIT;
        END IF;

        RAISE;
END;
/