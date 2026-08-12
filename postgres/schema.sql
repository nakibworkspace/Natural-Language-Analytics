-- =============================================================================
-- Ride-Sharing AI Analytics Lab — PostgreSQL schema
-- =============================================================================
-- This file is mounted to /docker-entrypoint-initdb.d/01-schema.sql so it runs
-- the first time the postgres container starts. It owns:
--   - The domain tables (riders, drivers, locations, rides, reviews)
--   - Useful indexes for analytical queries
--   - Comments documenting the business meaning of columns
--
-- The read-only role used by the skills is created in 03-readonly.sql.
-- The synthetic data is loaded in 02-seed.sql.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- -----------------------------------------------------------------------------
-- Locations (pickup / destination zones)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS locations (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    area          TEXT NOT NULL,                  -- e.g., 'North Dhaka'
    latitude      NUMERIC(9, 6),
    longitude     NUMERIC(9, 6),
    popularity    NUMERIC(4, 2) DEFAULT 1.00      -- multiplier used by seed
);

-- -----------------------------------------------------------------------------
-- Riders
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS riders (
    id            SERIAL PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,           -- synthetic id, e.g., 'R-00042'
    signup_date   DATE NOT NULL,
    age_band      TEXT,                           -- '18-24', '25-34', '35-44', '45+'
    segment       TEXT                            -- 'occasional', 'regular', 'premium'
);

-- -----------------------------------------------------------------------------
-- Drivers
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS drivers (
    id            SERIAL PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,           -- 'D-00123'
    name          TEXT NOT NULL,
    signup_date   DATE NOT NULL,
    rating        NUMERIC(3, 2) NOT NULL,         -- 1.00 - 5.00
    vehicle_type  TEXT NOT NULL                   -- 'sedan', 'suv', 'bike', 'tuktuk'
);

-- -----------------------------------------------------------------------------
-- Rides
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rides (
    id                     BIGSERIAL PRIMARY KEY,
    rider_id               INTEGER NOT NULL REFERENCES riders(id),
    driver_id              INTEGER NOT NULL REFERENCES drivers(id),
    pickup_location_id     INTEGER NOT NULL REFERENCES locations(id),
    destination_location_id INTEGER NOT NULL REFERENCES locations(id),
    fare                   NUMERIC(10, 2) NOT NULL,
    distance_km            NUMERIC(6, 2) NOT NULL,
    status                 TEXT NOT NULL,         -- 'completed','cancelled','requested','in_progress'
    requested_at           TIMESTAMPTZ NOT NULL,
    completed_at           TIMESTAMPTZ,
    cancelled_at           TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rides_status ON rides(status);
CREATE INDEX IF NOT EXISTS idx_rides_requested_at ON rides(requested_at);
CREATE INDEX IF NOT EXISTS idx_rides_destination ON rides(destination_location_id);
CREATE INDEX IF NOT EXISTS idx_rides_driver ON rides(driver_id);
CREATE INDEX IF NOT EXISTS idx_rides_rider ON rides(rider_id);

-- -----------------------------------------------------------------------------
-- Reviews
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS reviews (
    id            BIGSERIAL PRIMARY KEY,
    ride_id       BIGINT NOT NULL REFERENCES rides(id) ON DELETE CASCADE,
    rating        INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    comment       TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reviews_ride ON reviews(ride_id);
CREATE INDEX IF NOT EXISTS idx_reviews_rating ON reviews(rating);

-- -----------------------------------------------------------------------------
-- Helpful views (read-only by default; safer than letting the LLM write SQL on
-- raw base tables for any question that is purely analytical)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_rides AS
SELECT
    r.id,
    r.status,
    r.fare,
    r.distance_km,
    r.requested_at,
    r.completed_at,
    r.cancelled_at,
    EXTRACT(HOUR FROM r.requested_at AT TIME ZONE 'UTC') AS hour_of_day,
    DATE_TRUNC('day',   r.requested_at) AS day,
    DATE_TRUNC('week',  r.requested_at) AS week,
    DATE_TRUNC('month', r.requested_at) AS month,
    p.id  AS pickup_id,        p.name  AS pickup_location,        p.area AS pickup_area,
    d.id  AS destination_id,  d.name  AS destination_location,  d.area AS destination_area,
    dr.id AS driver_id,        dr.code AS driver_code,            dr.rating AS driver_baseline_rating,
    ri.id AS rider_id,         ri.code AS rider_code
FROM rides r
JOIN locations p  ON p.id  = r.pickup_location_id
JOIN locations d  ON d.id  = r.destination_location_id
JOIN drivers   dr ON dr.id = r.driver_id
JOIN riders    ri ON ri.id = r.rider_id;

COMMIT;
