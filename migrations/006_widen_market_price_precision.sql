-- 006_widen_market_price_precision.sql
--
-- Purpose:
--   Preserve the unrestricted precision of legacy ADMIN.STOCK_HISTORY
--   CLOSE_PRICE values in the canonical market-price table.
--
-- Safety:
--   This metadata-only change preserves existing rows. Migration 007 performs
--   the separate, guarded rebackfill needed to restore previously rounded
--   values.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions.

ALTER TABLE mk_daily_price
    MODIFY (close_price NUMBER);