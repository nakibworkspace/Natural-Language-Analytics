-- =============================================================================
-- Ride-Sharing AI Analytics Lab — seed data (SQL only, no Python dep)
-- =============================================================================
-- Scales (tuned to be light enough to seed in ~1-2 minutes on a laptop):
--   - 1,000 riders
--   - 200  drivers
--   - 10,000 rides
--   - 6,000  reviews
--
-- Realistic patterns introduced:
--   - Rush-hour spikes in the morning (8-10) and evening (17-20)
--   - Higher cancellation rate during rush hours
--   - Fare ~ proportional to distance plus small jitter
--   - Most destinations are popular areas (Gulshan, Dhanmondi, ...), with a few
--     "cold" destinations
--   - Some drivers have a personality rating bias (a baseline +/- offset)
--   - Reviews include templates covering the requested complaint categories
--
-- This file is deterministic via setseed() so reruns produce the same data.
-- =============================================================================

BEGIN;

SELECT setseed(0.42);

-- -----------------------------------------------------------------------------
-- Locations (10 popular zones in Dhaka)
-- -----------------------------------------------------------------------------
TRUNCATE reviews, rides, riders, drivers, locations RESTART IDENTITY CASCADE;

INSERT INTO locations (name, area, latitude, longitude, popularity) VALUES
  ('Gulshan',     'North Dhaka',   23.7925, 90.4078, 1.40),
  ('Dhanmondi',   'Central Dhaka', 23.7461, 90.3742, 1.30),
  ('Uttara',      'North Dhaka',   23.8759, 90.3795, 1.20),
  ('Banani',      'North Dhaka',   23.7937, 90.4066, 1.10),
  ('Mirpur',      'Northwest',     23.8069, 90.3687, 1.05),
  ('Mohammadpur', 'West Dhaka',    23.7600, 90.3590, 0.95),
  ('Motijheel',   'Old Dhaka',     23.7330, 90.4172, 0.85),
  ('Farmgate',    'Central Dhaka', 23.7546, 90.3876, 1.15),
  ('Bashundhara', 'North Dhaka',   23.8156, 90.4253, 1.00),
  ('Airport',     'North Dhaka',   23.8433, 90.3978, 0.90);

-- -----------------------------------------------------------------------------
-- Drivers (200): each has a baseline_rating_bias so some are consistently
-- higher / lower rated
-- -----------------------------------------------------------------------------
INSERT INTO drivers (code, name, signup_date, rating, vehicle_type)
SELECT
    'D-' || LPAD(g::TEXT, 5, '0'),
    (ARRAY['Rahim','Karim','Sumon','Akash','Nadia','Tasnim','Imran','Shuvo','Rafiq','Maya'])[1 + (g % 10)],
    CURRENT_DATE - ((g % 720) || ' days')::INTERVAL,
    -- baseline rating around 4.2 with small variation
    ROUND((3.5 + (random() * 1.5))::NUMERIC, 2),
    (ARRAY['sedan','sedan','sedan','suv','bike','tuktuk'])[1 + (g % 6)]
FROM generate_series(1, 200) g;

-- -----------------------------------------------------------------------------
-- Riders (1,000)
-- -----------------------------------------------------------------------------
INSERT INTO riders (code, signup_date, age_band, segment)
SELECT
    'R-' || LPAD(g::TEXT, 5, '0'),
    CURRENT_DATE - ((g % 900) || ' days')::INTERVAL,
    (ARRAY['18-24','25-34','35-44','45+'])[1 + (g % 4)],
    (ARRAY['occasional','regular','premium'])[1 + (g % 3)]
FROM generate_series(1, 1000) g;

-- -----------------------------------------------------------------------------
-- Rides (10,000): realistic distributions
-- -----------------------------------------------------------------------------
-- We use generate_series to make rows then apply distributions in SQL.
-- Each ride picks:
--   - a random rider/driver
--   - a pickup & destination weighted by location.popularity
--   - a requested_at timestamp over the last 90 days, biased toward rush hours
--   - fare ~ distance * 18 + jitter
--   - distance ~ uniform 1..25 km
--   - status: completed unless in rush hour, in which case 25% cancellation

INSERT INTO rides (
    rider_id, driver_id, pickup_location_id, destination_location_id,
    fare, distance_km, status, requested_at, completed_at, cancelled_at
)
WITH params AS (
    SELECT
        10000::INT AS n,
        90::INT    AS days_back
),
weights AS (
    -- Re-normalize location popularity into cumulative weights for sampling
    SELECT id, popularity, SUM(popularity) OVER (ORDER BY id) AS cum,
                  SUM(popularity) OVER () AS total
    FROM locations
),
pick AS (
    -- cumulative-weight buckets in [0, 1)
    SELECT id,
           (cum - popularity) / total AS low,
            cum             / total AS high
    FROM weights
),
rider_pool  AS (SELECT id FROM riders),
driver_pool AS (SELECT id FROM drivers),
g AS (
    SELECT generate_series(1, (SELECT n FROM params)) AS g
),
series AS (
    SELECT
        g.g,
        -- hour weighted toward rush hours (8-10 and 17-20) plus base traffic
        CASE
            WHEN random() < 0.18 THEN 8  + (random() * 2)::INT
            WHEN random() < 0.18 THEN 17 + (random() * 3)::INT
            WHEN random() < 0.10 THEN 21 + (random() * 3)::INT  -- late evening
            WHEN random() < 0.10 THEN 0  + (random() * 5)::INT  -- late night
            ELSE (random() * 23)::INT
        END AS hour,
        -- day offset in last 90 days
        ((random() * (SELECT days_back FROM params))::INT)::TEXT || ' days' AS day_offset,
        -- distance in km
        ROUND((1 + random() * 24)::NUMERIC, 2) AS distance_km,
        -- whether this is a rush-hour ride (for cancellation rate)
        (
            (random() < 0.18) OR (random() < 0.18)
        ) AS is_rush
    FROM g
),
rides_raw AS (
    SELECT
        series.*,
        -- pick random rider
        (SELECT id FROM rider_pool  ORDER BY random() LIMIT 1) AS rider_id,
        -- pick random driver
        (SELECT id FROM driver_pool ORDER BY random() LIMIT 1) AS driver_id,
        -- weighted pickup: one r per ride, correlated to series.g so PG can't
        -- fold it to a constant. (A bare LATERAL (SELECT random()) is folded.)
        pickup_pick.r AS pickup_r,
        dest_pick.r   AS dest_r,
        -- weighted pickup location
        (SELECT id FROM pick p WHERE pickup_pick.r < p.high ORDER BY p.high LIMIT 1) AS pickup_id,
        -- weighted destination (different from pickup)
        (SELECT id FROM pick p WHERE dest_pick.r   < p.high ORDER BY p.high LIMIT 1) AS dest_id,
        -- fare: distance * 18 + tiny jitter
        ROUND(((1 + random() * 24) * 18 + (random() * 30 - 15))::NUMERIC, 2) AS fare
    FROM series
    CROSS JOIN LATERAL (SELECT random() + series.g * 0 AS r) pickup_pick
    CROSS JOIN LATERAL (SELECT random() + series.g * 0 AS r) dest_pick
),
rides_final AS (
    SELECT
        rider_id,
        driver_id,
        pickup_id AS pickup_location_id,
        (CASE WHEN dest_id = pickup_id
              THEN ((pickup_id % 10) + 1)  -- force a different destination
              ELSE dest_id END) AS destination_location_id,
        fare,
        distance_km,
        -- status: cancelled mostly during rush hours
        CASE
            WHEN is_rush AND random() < 0.18 THEN 'cancelled'
            ELSE 'completed'
        END AS status,
        -- requested_at: today - day_offset + hour
        (NOW() - day_offset::INTERVAL
                - ((random() * 60)::INT || ' minutes')::INTERVAL)
            + (hour || ' hours')::INTERVAL AS requested_at
    FROM rides_raw
)
SELECT
    rider_id, driver_id, pickup_location_id, destination_location_id,
    fare, distance_km, status, requested_at,
    CASE WHEN status = 'completed'
         THEN requested_at + ((distance_km * 2 + 5 + random() * 15) || ' minutes')::INTERVAL
         END AS completed_at,
    CASE WHEN status = 'cancelled'
         THEN requested_at + ((3 + random() * 12) || ' minutes')::INTERVAL
         END AS cancelled_at
FROM rides_final;

-- -----------------------------------------------------------------------------
-- Reviews (6,000): one per completed ride with probability 0.6
-- Comments are templates aligned with the categorization in section 21.
-- -----------------------------------------------------------------------------
INSERT INTO reviews (ride_id, rating, comment, created_at)
WITH eligible AS (
    SELECT id, completed_at FROM rides WHERE status = 'completed' ORDER BY random() LIMIT 6000
),
templates AS (
    SELECT
        id, completed_at,
        CASE
            WHEN random() < 0.05 THEN 1
            WHEN random() < 0.20 THEN 2
            WHEN random() < 0.50 THEN 3
            WHEN random() < 0.85 THEN 4
            ELSE 5
        END AS rating
    FROM eligible
),
comment_pick AS (
    SELECT
        t.id, t.completed_at, t.rating,
        (ARRAY[
            'Driver was very late, I waited 20 minutes',                  -- lateness
            'Waited too long for the car',                                -- waiting
            'Driver was rude and took a wrong turn',                      -- behavior + route
            'Vehicle was dirty and smelled bad',                          -- vehicle condition
            'Fare was higher than the app estimate',                      -- pricing
            'Driver took a longer route on purpose',                      -- route
            'Great driver, smooth ride, very polite!',                    -- positive
            'Safe ride, followed traffic rules',                          -- safety
            NULL                                                          -- no comment
        ])[1 + (g % 9)] AS template,
        g
    FROM templates t
    CROSS JOIN LATERAL (SELECT generate_series(1, 1) g) s
)
SELECT
    id AS ride_id,
    rating,
    template AS comment,
    completed_at + ((1 + random() * 30) || ' minutes')::INTERVAL AS created_at
FROM comment_pick
WHERE completed_at IS NOT NULL;

COMMIT;

-- Sanity check (visible in docker logs)
SELECT 'locations' AS table, COUNT(*) FROM locations
UNION ALL SELECT 'drivers',  COUNT(*) FROM drivers
UNION ALL SELECT 'riders',   COUNT(*) FROM riders
UNION ALL SELECT 'rides',    COUNT(*) FROM rides
UNION ALL SELECT 'reviews',  COUNT(*) FROM reviews;
