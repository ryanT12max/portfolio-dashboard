-- 011_update_re_investment_analysis_scope.sql
--
-- Purpose:
--   Make the canonical real-estate reporting view scope-aware.  RentCast
--   returns a single-unit rent for Multi-Family listings, so that value must
--   never be used to calculate a whole-building gross yield.
--
-- Prerequisite:
--   Run migration 010 first.
--
-- Run once while connected as PORTFOLIO_APP in Oracle Database Actions.

CREATE OR REPLACE VIEW vw_re_current_investment_analysis AS
    WITH latest_attempt AS (
        SELECT
            attempted.property_key,
            attempted.attempt_status,
            attempted.attempted_at,
            attempted.error_category,
            ROW_NUMBER() OVER (
                PARTITION BY attempted.property_key
                ORDER BY attempted.attempted_at DESC,
                    attempted.rent_estimate_attempt_id DESC
            ) AS row_rank
        FROM re_rent_estimate_attempt attempted
        WHERE attempted.estimator_source = 'RENTCAST_RENT_AVM'
    )
    SELECT
        listing.property_key,
        listing.source_name AS listing_source_name,
        listing.source_property_id,
        listing.street_address,
        listing.city,
        listing.state_code,
        listing.zip_code,
        listing.property_type,
        listing.bedrooms,
        listing.bathrooms,
        listing.structure_sqft,
        listing.lot_size_sqft,
        listing.year_built,
        listing.has_deed_restrictions,
        listing.latitude,
        listing.longitude,
        listing.snapshot_id AS listing_snapshot_id,
        listing.observed_at AS listing_observed_at,
        listing.listing_date,
        listing.last_seen_date,
        listing.listing_status,
        listing.asking_price,
        estimate.rent_estimate_snapshot_id,
        estimate.run_id AS rent_estimate_run_id,
        estimate.observed_at AS rent_estimate_observed_at,
        estimate.estimator_source,
        estimate.estimate_scope,
        estimate.input_method,
        estimate.estimated_monthly_rent,
        estimate.rent_range_low,
        estimate.rent_range_high,
        estimate.comparable_count,
        CASE
            WHEN estimate.rent_estimate_snapshot_id IS NULL
                AND latest_attempt.attempt_status = 'UNAVAILABLE' THEN 'UNAVAILABLE'
            WHEN estimate.rent_estimate_snapshot_id IS NULL
                AND latest_attempt.attempt_status = 'TRANSIENT_FAILURE' THEN 'RETRY_PENDING'
            WHEN estimate.rent_estimate_snapshot_id IS NULL THEN 'MISSING'
            WHEN estimate.estimate_scope = 'UNIT' THEN 'ESTIMATED_UNIT'
            ELSE 'ESTIMATED_PROPERTY'
        END AS rent_estimate_status,
        CASE
            WHEN estimate.estimate_scope = 'PROPERTY' THEN 'Y'
            ELSE 'N'
        END AS gross_yield_eligible,
        CASE
            WHEN listing.asking_price IS NULL THEN 'MISSING_PRICE'
            WHEN estimate.rent_estimate_snapshot_id IS NULL
                AND latest_attempt.attempt_status = 'UNAVAILABLE' THEN 'RENT_UNAVAILABLE'
            WHEN estimate.rent_estimate_snapshot_id IS NULL THEN 'MISSING_RENT'
            WHEN estimate.estimate_scope = 'UNIT' THEN 'UNIT_RENT_ONLY'
            ELSE 'READY'
        END AS investment_analysis_status,
        CASE
            WHEN estimate.estimated_monthly_rent IS NULL THEN NULL
            ELSE estimate.estimated_monthly_rent * 12
        END AS estimated_annual_rent,
        CASE
            WHEN estimate.rent_range_low IS NULL THEN NULL
            ELSE estimate.rent_range_low * 12
        END AS estimated_annual_rent_low,
        CASE
            WHEN estimate.rent_range_high IS NULL THEN NULL
            ELSE estimate.rent_range_high * 12
        END AS estimated_annual_rent_high,
        CASE
            WHEN estimate.estimate_scope = 'PROPERTY' THEN ROUND(
                (estimate.estimated_monthly_rent * 12 / NULLIF(listing.asking_price, 0)) * 100,
                2
            )
        END AS estimated_gross_yield_pct,
        CASE
            WHEN estimate.estimate_scope = 'PROPERTY' THEN ROUND(
                (estimate.rent_range_low * 12 / NULLIF(listing.asking_price, 0)) * 100,
                2
            )
        END AS estimated_gross_yield_low_pct,
        CASE
            WHEN estimate.estimate_scope = 'PROPERTY' THEN ROUND(
                (estimate.rent_range_high * 12 / NULLIF(listing.asking_price, 0)) * 100,
                2
            )
        END AS estimated_gross_yield_high_pct,
        latest_attempt.attempt_status AS latest_rent_attempt_status,
        latest_attempt.attempted_at AS latest_rent_attempted_at,
        latest_attempt.error_category AS latest_rent_attempt_error_category
    FROM vw_re_current_listing listing
    LEFT JOIN vw_re_current_rent_estimate estimate
        ON estimate.property_key = listing.property_key
    LEFT JOIN latest_attempt
        ON latest_attempt.property_key = listing.property_key
       AND latest_attempt.row_rank = 1;