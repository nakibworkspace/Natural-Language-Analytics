# Postgres — schema, seed, and read-only role

This directory owns the database side of the lab.

## Files

| File              | Purpose                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| `schema.sql`      | Creates tables, indexes, and the `v_rides` view. Runs on first boot.   |
| `seed.sql`        | SQL-only deterministic seed (10k rides, 6k reviews). Runs on first boot. |
| `readonly_role.sql` | Creates the `analytics_reader` read-only role used by the skills.    |
| `seed.py`         | Optional Python seeder for re-seeding at different scales.             |

## What runs automatically

The `docker-compose.yml` mounts these files into
`/docker-entrypoint-initdb.d/`, so on the first time the Postgres container
starts:

1. `01-schema.sql` — creates tables, indexes, and the view.
2. `02-seed.sql` — populates the dataset.
3. `03-readonly.sql` — creates the read-only role.

The order is alphabetic, and the scripts are idempotent enough to survive a
restart of the container (they use `CREATE TABLE IF NOT EXISTS`,
`TRUNCATE ... RESTART IDENTITY`, etc.).

## What the skills connect as

The skills **MUST** use `analytics_reader`, never the superuser. The plumbing
that enforces this lives in `tools/postgres.py` (it reads
`POSTGRES_READER_USER`/`POSTGRES_READER_PASSWORD` from `.env`).

## Re-seeding

```bash
# Tiny / small / medium / large
docker compose exec postgres python -c "import sys; sys.path.insert(0,'/workspace/postgres'); from seed import seed; seed('medium')"
# OR from the host (if you mount the repo into the container)
python postgres/seed.py --scale medium
```

## Common manual queries

```sql
-- Top destinations
SELECT name, COUNT(*) AS rides
FROM rides r JOIN locations l ON l.id = r.destination_location_id
GROUP BY name ORDER BY rides DESC LIMIT 10;

-- Average fare by destination
SELECT l.name, ROUND(AVG(r.fare), 2) AS avg_fare
FROM rides r JOIN locations l ON l.id = r.destination_location_id
WHERE r.status = 'completed'
GROUP BY l.name ORDER BY avg_fare DESC;

-- Cancellation rate
SELECT
  COUNT(*) FILTER (WHERE status = 'cancelled')::FLOAT / COUNT(*) AS cancel_rate
FROM rides;

-- Per-hour traffic
SELECT EXTRACT(HOUR FROM requested_at) AS h, COUNT(*) FROM rides GROUP BY 1 ORDER BY 1;
```
