-- 003_create_re_investment_analysis_view.sql
--
-- Creates a dashboard-ready current-state view. This migration creates one
-- view only; it does not alter tables or write any data.

CREATE OR REPLACE VIEW vw_re_current_investment_analysis AS
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
            WHEN estimate.rent_estimate_snapshot_id IS NULL THEN 'MISSING'
            ELSE 'ESTIMATED'
        END AS rent_estimate_status,
        CASE
            WHEN listing.asking_price IS NULL THEN 'MISSING_PRICE'
            WHEN estimate.rent_estimate_snapshot_id IS NULL THEN 'MISSING_RENT'
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
        ROUND(
            (estimate.estimated_monthly_rent * 12 / NULLIF(listing.asking_price, 0)) * 100,
            2
        ) AS estimated_gross_yield_pct,
        ROUND(
            (estimate.rent_range_low * 12 / NULLIF(listing.asking_price, 0)) * 100,
            2
        ) AS estimated_gross_yield_low_pct,
        ROUND(
            (estimate.rent_range_high * 12 / NULLIF(listing.asking_price, 0)) * 100,
            2
        ) AS estimated_gross_yield_high_pct
    FROM vw_re_current_listing listing
    LEFT JOIN vw_re_current_rent_estimate estimate
        ON estimate.property_key = listing.property_key;