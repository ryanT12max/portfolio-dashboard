-- 013_enrich_market_security_reference.sql
--
-- Purpose:
--   Add a small, canonical security-reference layer to the market watchlist.
--   It stores the familiar company name and a reviewed broad-sector label for
--   the existing 44-security watchlist. The dashboard can then render useful
--   labels without issuing live provider calls on every page load.
--
-- Provenance:
--   This is a reviewed, curated reference seed. The stored source explicitly
--   says CURATED_WATCHLIST_REFERENCE; it does not claim to be a live provider
--   feed or a provider reporting date. A future provider-backed security-master
--   ETL can replace these fields while retaining the same canonical columns.
--
-- Safety:
--   * Alters only PORTFOLIO_APP.MK_SECURITY.
--   * Updates only the 44 tickers already in the canonical watchlist.
--   * Does not alter, delete, or read any ADMIN-owned legacy object.
--   * Replaces only the PORTFOLIO_APP reporting view used by the dashboard.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions.

ALTER TABLE mk_security ADD (
    security_name                       VARCHAR2(200),
    security_reference_source           VARCHAR2(50),
    security_reference_observed_at      TIMESTAMP WITH TIME ZONE,
    security_reference_payload_hash     VARCHAR2(64)
);

MERGE INTO mk_security target
USING (
    SELECT 'AAPL' AS ticker, 'Apple Inc.' AS security_name, 'Technology' AS sector FROM dual
    UNION ALL SELECT 'ABBV', 'AbbVie Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'ADBE', 'Adobe Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'AMAT', 'Applied Materials, Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'AMD', 'Advanced Micro Devices, Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'AMGN', 'Amgen Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'BKR', 'Baker Hughes Company', 'Energy' FROM dual
    UNION ALL SELECT 'BMY', 'Bristol-Myers Squibb Company', 'Healthcare' FROM dual
    UNION ALL SELECT 'CI', 'The Cigna Group', 'Healthcare' FROM dual
    UNION ALL SELECT 'COP', 'ConocoPhillips', 'Energy' FROM dual
    UNION ALL SELECT 'CRM', 'Salesforce, Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'CSCO', 'Cisco Systems, Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'CVS', 'CVS Health Corporation', 'Healthcare' FROM dual
    UNION ALL SELECT 'CVX', 'Chevron Corporation', 'Energy' FROM dual
    UNION ALL SELECT 'DHR', 'Danaher Corporation', 'Healthcare' FROM dual
    UNION ALL SELECT 'EOG', 'EOG Resources, Inc.', 'Energy' FROM dual
    UNION ALL SELECT 'GILD', 'Gilead Sciences, Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'HAL', 'Halliburton Company', 'Energy' FROM dual
    UNION ALL SELECT 'IBM', 'International Business Machines Corporation', 'Technology' FROM dual
    UNION ALL SELECT 'INTC', 'Intel Corporation', 'Technology' FROM dual
    UNION ALL SELECT 'INTU', 'Intuit Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'ISRG', 'Intuitive Surgical, Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'JNJ', 'Johnson ' || CHR(38) || ' Johnson', 'Healthcare' FROM dual
    UNION ALL SELECT 'KMI', 'Kinder Morgan, Inc.', 'Energy' FROM dual
    UNION ALL SELECT 'LLY', 'Eli Lilly and Company', 'Healthcare' FROM dual
    UNION ALL SELECT 'MDT', 'Medtronic plc', 'Healthcare' FROM dual
    UNION ALL SELECT 'MPC', 'Marathon Petroleum Corporation', 'Energy' FROM dual
    UNION ALL SELECT 'MRK', 'Merck ' || CHR(38) || ' Co., Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'MSFT', 'Microsoft Corporation', 'Technology' FROM dual
    UNION ALL SELECT 'NOW', 'ServiceNow, Inc.', 'Technology' FROM dual
    UNION ALL SELECT 'NVDA', 'NVIDIA Corporation', 'Technology' FROM dual
    UNION ALL SELECT 'ORCL', 'Oracle Corporation', 'Technology' FROM dual
    UNION ALL SELECT 'OXY', 'Occidental Petroleum Corporation', 'Energy' FROM dual
    UNION ALL SELECT 'PFE', 'Pfizer Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'PSX', 'Phillips 66', 'Energy' FROM dual
    UNION ALL SELECT 'QCOM', 'QUALCOMM Incorporated', 'Technology' FROM dual
    UNION ALL SELECT 'SLB', 'SLB Limited', 'Energy' FROM dual
    UNION ALL SELECT 'TMO', 'Thermo Fisher Scientific Inc.', 'Healthcare' FROM dual
    UNION ALL SELECT 'TRGP', 'Targa Resources Corp.', 'Energy' FROM dual
    UNION ALL SELECT 'TXN', 'Texas Instruments Incorporated', 'Technology' FROM dual
    UNION ALL SELECT 'UNH', 'UnitedHealth Group Incorporated', 'Healthcare' FROM dual
    UNION ALL SELECT 'VLO', 'Valero Energy Corporation', 'Energy' FROM dual
    UNION ALL SELECT 'WMB', 'The Williams Companies, Inc.', 'Energy' FROM dual
    UNION ALL SELECT 'XOM', 'Exxon Mobil Corporation', 'Energy' FROM dual
) source
ON (target.ticker = source.ticker)
WHEN MATCHED THEN
    UPDATE SET
        target.security_name = source.security_name,
        target.sector = source.sector,
        target.security_reference_source = 'CURATED_WATCHLIST_REFERENCE',
        target.security_reference_observed_at = SYSTIMESTAMP,
        target.security_reference_payload_hash = RAWTOHEX(STANDARD_HASH(
            source.ticker || '|' || source.security_name || '|' || source.sector,
            'SHA256'
        )),
        target.updated_at = SYSTIMESTAMP;

CREATE OR REPLACE VIEW vw_mk_watchlist_analysis AS
    SELECT
        security.security_key,
        security.ticker,
        security.security_name,
        security.sector,
        security.security_reference_source,
        security.security_reference_observed_at,
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

COMMIT;

-- Verification queries (run separately after the migration succeeds):
--
-- SELECT
--     COUNT(*) AS watchlist_securities,
--     COUNT(security_name) AS securities_with_name,
--     COUNT(sector) AS securities_with_sector,
--     COUNT(security_reference_payload_hash) AS securities_with_reference_hash
-- FROM mk_security;
--
-- SELECT sector, COUNT(*) AS security_count
-- FROM mk_security
-- GROUP BY sector
-- ORDER BY sector;
--
-- SELECT
--     object_name,
--     object_type,
--     status
-- FROM user_objects
-- WHERE object_name = 'VW_MK_WATCHLIST_ANALYSIS';