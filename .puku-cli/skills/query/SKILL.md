---
name: query
description: Translate a natural-language question about RIDE-SHARING data (Postgres db ride_analytics) into SQL, run it, and return a structured QueryResult with columns + rows + analysis hints. Use when the user wants only the data — no chart, no dashboard. Trigger phrases: "query", "run this question", "what's the SQL for ...", "give me the rows", "show me the data", "how many rides ...". Use for: rides, drivers, fare, cancellation, revenue, rating, destination, pickup, dropoff, locations (Airport, Banani, Bashundhara, Dhanmondi, Farmgate, Gulshan, Mirpur, Mohammadpur, Motijheel, Uttara). NOT for Loki / NetFlow / DNS / network-traffic analysis.
allowed-tools:
  - Bash(python:*)
  - Read
when_to_use: |
  Use ONLY for ride-sharing analytical questions against this repo's
  Postgres database (ride_analytics). Returns rows + SQL, no dashboard.
  Do NOT use for network traffic analysis (netflow-traffic), DNS queries
  (loki-ai-sites), or any Loki / Grafana query.
argument-hint: "<question>"
arguments:
  - question
context: fork
---

# /query — natural-language → SQL → rows

This skill runs ``tools/query.py`` to translate a natural-language
question into SQL and execute it against the read-only Postgres role.

## Steps

### 1. Run the Query skill
```bash
cd /Users/nakibahmed/workspace/poridhi-workspace/fde-poc/ride-sharing && \
  python3 tools/query.py "$question"
```

**Success criteria**:
- Exit code 0.
- Last line of stdout is a JSON ``QueryResult`` with ``sql``, ``columns``,
  ``rows``, ``row_count``, ``analysis``.

### 2. Surface the result
- Print the SQL in a fenced block.
- Print the first 20 rows as a markdown table (or fewer if ``row_count``
  is smaller).
- If exit is 2 (offline stub didn't recognise the question), surface the
  stderr verbatim and suggest trying with an LLM (`--use-llm`).

## Rules
- Do NOT call Postgres directly; always go through ``tools/query.py``.
- Never echo ``.env`` secrets.
- Use ``/dashboard`` instead if the user wants a chart.