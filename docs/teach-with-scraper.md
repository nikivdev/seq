# Teach With Scraper

Performance-first workflow for generating dependency skills from live docs.

## Why

- Keep `seq` coding context tight with fresh dependency docs.
- Reuse always-on scraper jobs (`/jobs`) instead of cold one-off fetches.
- Persist generated skills in repo-local `.ai/skills/generated`.

## Commands

```bash
# Verify scraper daemon/api is reachable
f scrape-health

# Generate a skill for one dependency
f teach-dep react

# Force ecosystem when needed
f teach-dep --ecosystem cargo tokio

# Generate from URL(s)
f teach-url https://docs.python.org/3/library/asyncio.html --name asyncio

# Auto-discover dependencies from package manifests
f teach-auto --top 2
```

## Output

Each target writes:

- `.ai/skills/generated/<slug>/SKILL.md`
- `.ai/skills/generated/<slug>/sources.json`

Skills include:

- install hint by ecosystem
- extracted headings and quick notes
- source provenance with timings and cache status

## Performance Notes

- Uses queued scraper jobs (`POST /jobs`, `GET /jobs/{id}`) for non-blocking fetch.
- Adaptive polling (fast when jobs complete, slower under load).
- On-disk cache at `.ai/internal/teach-cache.json` (24h TTL by default).
- `--force` bypasses cache for fresh pulls.
- Optional direct fallback (`--allow-direct-fallback`) when scraper is down.

## Observability

`tools/teach_deps.py` now emits best-effort `teach.*` events into:

- `${SEQ_CH_MEM_PATH:-~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl}`

Event names:

- `teach.run.start`
- `teach.scrape.enqueue`
- `teach.scrape.done`
- `teach.scrape.error`
- `teach.skill.generated`
- `teach.run.done`

Quick stream:

```bash
f teach-logs
```

Disable emission for one run:

```bash
f teach-dep react --no-mem-events
```

### ClickHouse Queries

If you already created `v_seq_mem` from `docs/chdig.md`, run:

```sql
-- p50/p95 durations (milliseconds) by teach event
SELECT
  name,
  count() AS n,
  round(quantileTDigest(0.50)(dur_us / 1000.0), 2) AS p50_ms,
  round(quantileTDigest(0.95)(dur_us / 1000.0), 2) AS p95_ms
FROM v_seq_mem
WHERE name LIKE 'teach.%'
GROUP BY name
ORDER BY n DESC;
```

```sql
-- cache hit ratio for scrape completions
SELECT
  count() AS total,
  countIf(position(ifNull(subject, ''), 'cache_hit=1') > 0) AS cache_hits,
  round(cache_hits / nullIf(total, 0), 4) AS cache_hit_ratio
FROM v_seq_mem
WHERE name = 'teach.scrape.done';
```

```sql
-- success/failure by dependency + ecosystem
SELECT
  extract(ifNull(subject, ''), 'dependency=([^\\t]+)') AS dependency,
  extract(ifNull(subject, ''), 'ecosystem=([^\\t]+)') AS ecosystem,
  countIf(ok) AS success_count,
  countIf(NOT ok) AS failure_count
FROM v_seq_mem
WHERE name = 'teach.skill.generated'
GROUP BY dependency, ecosystem
ORDER BY failure_count DESC, success_count DESC;
```

## Env

```bash
# Optional overrides
export SEQ_SCRAPER_BASE_URL=http://127.0.0.1:7444
export SEQ_SCRAPER_API_KEY=
export SEQ_CH_MEM_PATH=~/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl
```
