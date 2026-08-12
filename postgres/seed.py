"""Optional Python seeder for the ride-sharing analytics database.

This is a parallel implementation to ``seed.sql``. It is **not required** for the
lab — ``docker compose up`` will already load the SQL seed automatically. This
script is provided so that a learner can:

  * Override the default scale (10k rides etc. -> larger datasets)
  * Re-seed a running container without dropping the DB
  * Experiment with different distributions

Usage:
    python postgres/seed.py --scale large
    python postgres/seed.py --scale small

It uses the **superuser** from .env to TRUNCATE/INSERT, so it is intentionally
NOT something the skills can call.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
from dotenv import load_dotenv
import os

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

SCALES = {
    "tiny":   dict(riders=200,  drivers=50,  rides=2_000,   reviews=1_200),
    "small":  dict(riders=1_000, drivers=200, rides=10_000, reviews=6_000),
    "medium": dict(riders=5_000, drivers=500, rides=50_000, reviews=30_000),
    "large":  dict(riders=10_000, drivers=1_000, rides=100_000, reviews=60_000),
}

LOCATIONS = [
    # name,          area,               lat,     lon,     popularity
    ("Gulshan",     "North Dhaka",   23.7925, 90.4078, 1.40),
    ("Dhanmondi",   "Central Dhaka", 23.7461, 90.3742, 1.30),
    ("Uttara",      "North Dhaka",   23.8759, 90.3795, 1.20),
    ("Banani",      "North Dhaka",   23.7937, 90.4066, 1.10),
    ("Mirpur",      "Northwest",     23.8069, 90.3687, 1.05),
    ("Mohammadpur", "West Dhaka",    23.7600, 90.3590, 0.95),
    ("Motijheel",   "Old Dhaka",     23.7330, 90.4172, 0.85),
    ("Farmgate",    "Central Dhaka", 23.7546, 90.3876, 1.15),
    ("Bashundhara", "North Dhaka",   23.8156, 90.4253, 1.00),
    ("Airport",     "North Dhaka",   23.8433, 90.3978, 0.90),
]

DRIVER_NAMES = ["Rahim","Karim","Sumon","Akash","Nadia","Tasnim","Imran","Shuvo","Rafiq","Maya"]
VEHICLES = ["sedan","sedan","sedan","suv","bike","tuktuk"]
AGE_BANDS = ["18-24","25-34","35-44","45+"]
SEGMENTS = ["occasional","regular","premium"]

REVIEW_TEMPLATES = [
    (1, "Driver was very late, I waited 20 minutes"),
    (2, "Waited too long for the car"),
    (2, "Driver was rude and took a wrong turn"),
    (3, "Vehicle was dirty and smelled bad"),
    (3, "Fare was higher than the app estimate"),
    (4, "Driver took a longer route on purpose"),
    (5, "Great driver, smooth ride, very polite!"),
    (5, "Safe ride, followed traffic rules"),
    (4, None),  # no comment, average rating
]

def weighted_pick(options):
    names, weights = zip(*options)
    return random.choices(names, weights=weights, k=1)[0]

def hour_pick():
    r = random.random()
    if r < 0.18: return random.randint(8, 10)
    if r < 0.36: return random.randint(17, 20)
    if r < 0.46: return random.randint(21, 23)
    if r < 0.56: return random.randint(0, 4)
    return random.randint(5, 16)

def seed(scale_name: str) -> None:
    scale = SCALES[scale_name]
    dsn = (
        f"host={os.getenv('POSTGRES_HOST','localhost')} "
        f"port={os.getenv('POSTGRES_PORT','5432')} "
        f"dbname={os.getenv('POSTGRES_DB','ride_analytics')} "
        f"user={os.getenv('POSTGRES_SUPERUSER','postgres')} "
        f"password={os.getenv('POSTGRES_SUPERUSER_PASSWORD','postgres')}"
    )
    print(f"[seed] connecting as superuser...")
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE reviews, rides, riders, drivers, locations RESTART IDENTITY CASCADE;")

            # Locations
            cur.executemany(
                "INSERT INTO locations (name, area, latitude, longitude, popularity) VALUES (%s,%s,%s,%s,%s);",
                LOCATIONS,
            )
            cur.execute("SELECT id, popularity FROM locations;")
            loc_rows = cur.fetchall()
            loc_ids = [r[0] for r in loc_rows]

            # Drivers
            driver_data = [
                (f"D-{i:05d}",
                 random.choice(DRIVER_NAMES),
                 datetime.utcnow().date() - timedelta(days=random.randint(0, 720)),
                 round(3.5 + random.random() * 1.5, 2),
                 random.choice(VEHICLES))
                for i in range(1, scale["drivers"] + 1)
            ]
            cur.executemany(
                "INSERT INTO drivers (code, name, signup_date, rating, vehicle_type) VALUES (%s,%s,%s,%s,%s);",
                driver_data,
            )
            cur.execute("SELECT id FROM drivers;")
            driver_ids = [r[0] for r in cur.fetchall()]

            # Riders
            rider_data = [
                (f"R-{i:05d}",
                 datetime.utcnow().date() - timedelta(days=random.randint(0, 900)),
                 random.choice(AGE_BANDS),
                 random.choice(SEGMENTS))
                for i in range(1, scale["riders"] + 1)
            ]
            cur.executemany(
                "INSERT INTO riders (code, signup_date, age_band, segment) VALUES (%s,%s,%s,%s);",
                rider_data,
            )
            cur.execute("SELECT id FROM riders;")
            rider_ids = [r[0] for r in cur.fetchall()]

            # Rides (use COPY for speed)
            print(f"[seed] generating {scale['rides']} rides...")
            ride_buf = []
            now = datetime.utcnow()
            for _ in range(scale["rides"]):
                hr = hour_pick()
                day_off = random.randint(0, 90)
                minute_off = random.randint(0, 59)
                requested_at = (now
                                - timedelta(days=day_off)
                                - timedelta(minutes=random.randint(0, 60))
                                + timedelta(hours=hr))
                distance = round(1 + random.random() * 24, 2)
                fare = round(distance * 18 + (random.random() * 30 - 15), 2)
                pickup = random.choice(loc_ids)
                dest = random.choice(loc_ids)
                while dest == pickup:
                    dest = random.choice(loc_ids)
                is_rush = hr in range(8, 11) or hr in range(17, 21)
                cancelled = is_rush and random.random() < 0.18
                completed = not cancelled
                completed_at = requested_at + timedelta(minutes=distance * 2 + 5 + random.random() * 15) if completed else None
                cancelled_at = requested_at + timedelta(minutes=3 + random.random() * 12) if cancelled else None
                ride_buf.append((
                    random.choice(rider_ids),
                    random.choice(driver_ids),
                    pickup, dest,
                    fare, distance,
                    "cancelled" if cancelled else "completed",
                    requested_at,
                    completed_at,
                    cancelled_at,
                ))
            # chunked COPY
            with cur.copy(
                "COPY rides (rider_id, driver_id, pickup_location_id, destination_location_id, "
                "fare, distance_km, status, requested_at, completed_at, cancelled_at) FROM STDIN"
            ) as copy:
                for row in ride_buf:
                    copy.write_row(row)

            # Reviews: ~60% of completed rides
            cur.execute("SELECT id, completed_at FROM rides WHERE status='completed';")
            completed = cur.fetchall()
            review_count = min(scale["reviews"], len(completed))
            chosen = random.sample(completed, review_count)
            review_buf = []
            for ride_id, completed_at in chosen:
                rating, comment = random.choice(REVIEW_TEMPLATES)
                review_buf.append((
                    ride_id, rating, comment,
                    completed_at + timedelta(minutes=random.randint(1, 30))
                ))
            with cur.copy("COPY reviews (ride_id, rating, comment, created_at) FROM STDIN") as copy:
                for row in review_buf:
                    copy.write_row(row)

        conn.commit()
    print("[seed] done.")

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scale", choices=SCALES.keys(), default="small")
    args = p.parse_args()
    seed(args.scale)

if __name__ == "__main__":
    sys.exit(main())
