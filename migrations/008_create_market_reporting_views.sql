-- 008_create_market_reporting_views.sql
--
-- Purpose:
--   Create PORTFOLIO_APP reporting views for the canonical market-research
--   model. These views have no dependency on the legacy ADMIN schema.
--
-- Safety:
--   This migration creates views only. It does not modify market rows,
--   execute an API request, or access ADMIN-owned tables.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions.

CREATE OR REPLACE VIEW vw_mk_current_fundamentals AS
    WITH ranked_fundamentals AS (
        SELECT
            fundamental.fundamental_snapshot_id,
            fundamental.security_key,
            fundamental.run_id,
            fundamental.source_name,
            fundamental.source_as_of_date,
            fundamental.observed_at,
            fundamental.market_price,
            fundamental.pe_ratio,
            fundamental.peg_ratio,
            fundamental.pb_ratio,
            fundamental.earnings_per_share,
            fundamental.return_on_equity_pct,
            fundamental.profit_margin_pct,
            fundamental.debt_to_equity_ratio,
            fundamental.free_cash_flow_value,
            fundamental.free_cash_flow_unit,
            ROW_NUMBER() OVER (
                PARTITION BY fundamental.security_key
                ORDER BY fundamental.observed_at DESC,
                    fundamental.fundamental_snapshot_id DESC
            ) AS row_rank
        FROM mk_fundamental_snapshot fundamental
        JOIN mk_etl_run run_audit
            ON run_audit.run_id = fundamental.run_id
           AND run_audit.run_status IN ('SUCCEEDED', 'PARTIAL')
    )
    SELECT
        security.security_key,
        security.ticker,
        security.sector,
        fundamentals.fundamental_snapshot_id,
        fundamentals.run_id,
        fundamentals.source_name,
        fundamentals.source_as_of_date,
        fundamentals.observed_at,
        fundamentals.market_price,
        fundamentals.pe_ratio,
        fundamentals.peg_ratio,
        fundamentals.pb_ratio,
        fundamentals.earnings_per_share,
        fundamentals.return_on_equity_pct,
        fundamentals.profit_margin_pct,
        fundamentals.debt_to_equity_ratio,
        fundamentals.free_cash_flow_value,
        fundamentals.free_cash_flow_unit
    FROM ranked_fundamentals fundamentals
    JOIN mk_security security
        ON security.security_key = fundamentals.security_key
    WHERE fundamentals.row_rank = 1;

CREATE OR REPLACE VIEW vw_mk_price_history AS
    SELECT
        security.security_key,
        security.ticker,
        security.sector,
        price.trade_date,
        price.close_price,
        price.volume,
        price.run_id,
        price.source_name,
        price.observed_at
    FROM mk_daily_price price
    JOIN mk_security security
        ON security.security_key = price.security_key
    JOIN mk_etl_run run_audit
        ON run_audit.run_id = price.run_id
       AND run_audit.run_status IN ('SUCCEEDED', 'PARTIAL');

CREATE OR REPLACE VIEW vw_mk_latest_price AS
    WITH ranked_prices AS (
        SELECT
            security_key,
            ticker,
            sector,
            trade_date,
            close_price,
            volume,
            run_id,
            source_name,
            observed_at,
            ROW_NUMBER() OVER (
                PARTITION BY security_key
                ORDER BY trade_date DESC, observed_at DESC
            ) AS row_rank
        FROM vw_mk_price_history
    )
    SELECT
        security_key,
        ticker,
        sector,
        trade_date,
        close_price,
        volume,
        run_id,
        source_name,
        observed_at
    FROM ranked_prices
    WHERE row_rank = 1;

CREATE OR REPLACE VIEW vw_mk_watchlist_analysis AS
    SELECT
        security.security_key,
        security.ticker,
        security.sector,
        latest_price.trade_date AS latest_trade_date,
        latest_price.close_price AS latest_close_price,
        latest_price.volume AS latest_volume,
        latest_price.source_name AS latest_price_source,
        latest_price.observed_at AS latest_price_observed_at,
        fundamentals.fundamental_snapshot_id,
        fundamentals.source_name AS fundamentals_source,
        fundamentals.source_as_of_date AS fundamentals_source_as_of_date,
        fundamentals.observed_at AS fundamentals_observed_at,
        fundamentals.market_price AS fundamental_market_price,
        fundamentals.pe_ratio,
        fundamentals.peg_ratio,
        fundamentals.pb_ratio,
        fundamentals.earnings_per_share,
        fundamentals.return_on_equity_pct,
        fundamentals.profit_margin_pct,
        fundamentals.debt_to_equity_ratio,
        fundamentals.free_cash_flow_value,
        fundamentals.free_cash_flow_unit,
        CASE
            WHEN latest_price.security_key IS NULL THEN 'MISSING_PRICE_HISTORY'
            ELSE 'PRICE_AVAILABLE'
        END AS price_history_status,
        CASE
            WHEN fundamentals.security_key IS NULL THEN 'MISSING_FUNDAMENTALS'
            WHEN fundamentals.source_as_of_date IS NULL THEN 'AVAILABLE_AS_OF_UNKNOWN'
            ELSE 'AVAILABLE'
        END AS fundamentals_status
    FROM mk_security security
    LEFT JOIN vw_mk_latest_price latest_price
        ON latest_price.security_key = security.security_key
    LEFT JOIN vw_mk_current_fundamentals fundamentals
        ON fundamentals.security_key = security.security_key;