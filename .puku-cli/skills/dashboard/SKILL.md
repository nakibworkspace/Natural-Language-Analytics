---
name: dashboard
description: ALWAYS USE THIS SKILL for any phrase mentioning a Dhaka area or ride-sharing metric: "traffic in <location>", "traffic at <location>", "rides in <location>", "rides from <location>", "rides to <location>", "top <n> destinations", "average fare", "cancellation rate", "rating distribution", "busiest hours", "top drivers", "revenue by destinations", "review counts". The phrase "Airport" in this context means Hazrat Shahjalal International Airport in Dhaka, Bangladesh — NOT a generic airport / flight / aviation query. All Dhaka areas: Gulshan, Banani, Bashundhara, Dhanmondi, Farmgate, Mirpur, Mohammadpur, Motijheel, Uttara, Airport. This is a LOCAL RIDE-SHARING ANALYTICS lab backed by Postgres `ride_analytics` — NOT Google Maps, NOT real-time road traffic, NOT aviation.
allowed-tools:
  - Bash(python:*)
  - Read
when_to_use: |
  Use for ANY ride-sharing question that wants a chart / dashboard /
  visualisation. Accepts either:
    - a natural-language question (chains the Query skill then builds a
      dashboard — the typical case), or
    - a path to a pre-computed QueryResult JSON file (skips the Query
      step — use this if the user already ran /query).
  Do NOT use for network traffic analysis (netflow-traffic), DNS queries
  (loki-ai-sites), or any Loki / Grafana query — even if the word
  "traffic" appears, ride-sharing traffic goes through THIS skill.
argument-hint: "<question-or-results-json> [dashboard-name]"
arguments:
  - question_or_json
  - dashboard_name (optional when input is a question)
---

# /dashboard — ride-sharing data → Metabase dashboard

This skill runs ``tools/dashboard.py``. It accepts either:

- A **natural-language question** (typical case) — chains the Query
  skill internally, then renders the result as a card on a Metabase
  dashboard and returns the URL.
- A **path to a QueryResult JSON file** — skips the Query step (use
  this when you've already run ``/query`` and want to add the same data
  to a dashboard).

## Steps

### Case A: Natural-language question

```bash
cd /Users/nakibahmed/workspace/poridhi-workspace/fde-poc/ride-sharing && \
  python3 tools/dashboard.py --include-answer "$question" 2>/dev/null \
  | awk 'NR==1 || NR==2' \
  | tee /tmp/dashboard_url.txt \
  | { read -r url; open "$url"; echo "$url"; }
```

### Case B: Pre-computed QueryResult JSON

```bash
cd /Users/nakibahmed/workspace/poridhi-workspace/fde-poc/ride-sharing && \
  python3 tools/dashboard.py <results-json-path> "$dashboard_name" 2>/dev/null \
  | awk 'NR==1' \
  | tee /tmp/dashboard_url.txt \
  | { read -r url; open "$url"; echo "$url"; }
```

The `{ read -r url; open "$url"; echo "$url"; }` block:
  - reads the URL from stdin (so the pipeline doesn't consume stdout),
  - opens it in the browser in the background,
  - echoes the URL back to stdout so puku sees it.

Replace `open` with `xdg-open` on Linux.

`/tmp/dashboard_url.txt` is the canonical URL store; the user can also
`cat` it directly to get the URL as text.

**stdout format** (printed in this order, one item per line):
1. The dashboard URL on its own line — e.g. `http://localhost:3000/dashboard/17`.
2. (Case A only) The 1-line answer text.

**Success criteria**:
- Exit code 0.
- The FIRST line of stdout is the dashboard URL.
- The SECOND line of stdout (Case A) is the answer.

### Output rules — non-negotiable

After running the bash command above, **you MUST Read
`/tmp/dashboard_url.txt`** so the URL becomes part of your working
memory. Then your reply to the user MUST be EXACTLY these lines, in
order, with NO additional text, NO summary, NO narrative, NO header:

Case A:
```
[Dashboard](<URL>)
<answer line>
```

Case B:
```
[Dashboard](<URL>)
```

Where `<URL>` is line 1 of the file you just read, and `<answer line>`
is line 2 (Case A) — or omit that line for Case B.

Concrete examples of what **NOT** to do (all of these are failures):
- ❌ `Dashboard` then a separate sentence about what was created.
- ❌ "A bar chart was added to dashboard #21. Open the link above."
- ❌ "The URL is on the first line of stdout" (the user cannot see stdout).
- ❌ Wrapping the URL in `[Click here](http://...)` and burying it.
- ❌ Skipping the Read step and trying to remember the URL from the
  bash output alone (you WILL forget it — puku summarises bash output
  and strips URLs).
- ❌ Re-running the bash command "just to check" — each rerun creates a
  new Metabase card on the dashboard. Trust `/tmp/dashboard_url.txt`.

### Rules
- Do NOT call Metabase or Postgres directly; always go through ``tools/dashboard.py``.
- Always Read `/tmp/dashboard_url.txt` after the bash step.
- Run the bash command EXACTLY ONCE per `/dashboard` invocation.
- Never echo ``.env`` secrets.
- If the user only wants the rows (no chart), use ``/query`` instead.
