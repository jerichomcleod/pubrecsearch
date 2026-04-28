# Operations Guide

This document covers how to set up, run, and maintain a PubRecSearch installation from scratch.

---

## System Requirements

- **Python:** 3.11 or newer
- **PostgreSQL:** 14 or newer, running locally
- **Disk:** ~400 GB free for full historical backfill (raw files in R2 are remote; local temp space needed during bootstrap)
- **RAM:** At minimum 8 GB. CMS bootstrap streams ~9 GB files and uses Polars lazy evaluation; peak RAM during a CMS run is typically 1–2 GB
- **OS:** macOS or Linux. Not tested on Windows

---

## First-Time Setup

### 1. Clone and install

```bash
git clone https://github.com/yourname/pubrecsearch.git
cd pubrecsearch
pip install -e ".[dev]"
```

This installs the `pubrecsearch` CLI entry point. All commands below assume the package is installed.

### 2. Create PostgreSQL database

```bash
createdb pubrecsearch
createuser pubrecsearch
psql -c "ALTER USER pubrecsearch WITH PASSWORD 'changeme';"
psql -c "GRANT ALL ON DATABASE pubrecsearch TO pubrecsearch;"
```

**Assumption:** PostgreSQL is running locally and `psql` is in PATH with appropriate superuser access.

### 3. Configure environment

Copy or create `.env` in the project root:

```ini
# PostgreSQL
DATABASE_URL=postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch

# Cloudflare R2
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY=<access-key-id>
R2_SECRET_KEY=<secret-access-key>
R2_BUCKET=pubrecsearch-raw

# Email alerts (optional — leave blank to disable)
RESEND_API_KEY=re_...
ALERT_EMAIL=you@example.com

# SAM.gov API key (free — register at sam.gov)
SAM_API_KEY=<key>

# SEC EDGAR User-Agent (required by SEC policy — use your real contact info)
EDGAR_USER_AGENT=YourName contact@yourdomain.com

# LDA Lobbying API key (free — register at lda.senate.gov)
LDA_API_KEY=<key>
```

`.env` is listed in `.gitignore` — never commit it.

**Assumption:** All credentials are read at startup via Pydantic's `BaseSettings`. Missing values fall back to empty strings or defaults — scrapers will fail at runtime (not at import time) if required credentials are missing.

### 4. Apply database schema

```bash
pubrecsearch init-db
```

This runs `db/schema.sql` against the configured database. The schema uses `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` — safe to run multiple times. It also creates the `pg_trgm` extension.

**What `init-db` creates:**
- Tables: `individuals`, `documents`, `individual_documents`, `scrape_jobs`, `scrape_state`, `scrape_errors`
- Indexes: trigram GIN on `name_norm`, GIN full-text on `name`, B-tree indexes on `source_id`, `file_hash`, etc.

### 5. Create R2 bucket

In the Cloudflare dashboard:
1. Create an R2 bucket named `pubrecsearch-raw` (or whatever you set in `R2_BUCKET`)
2. Create an R2 API token with "Object Read & Write" permission scoped to that bucket
3. Set the endpoint URL to `https://<account-id>.r2.cloudflarestorage.com`

The R2 bucket is the permanent archive of all raw source files. PostgreSQL can be rebuilt from it if needed (re-run bootstrap scripts; SHA-256 dedup will prevent re-downloading existing files).

---

## Running Scrapers

### Run a single scraper immediately

```bash
pubrecsearch run ofac_sdn
pubrecsearch run fec_contributions
```

Runs one full scrape cycle for the named source outside the scheduler. Useful for testing or manual triggering. Output is written to stdout as structured JSON logs.

### Run all scrapers immediately

```bash
pubrecsearch run
```

Iterates through all 14 scrapers in `ALL_SCRAPERS` order and runs each once. This will be slow for sources that normally only fetch incremental data (e.g., FEC will only check for new daily updates, not re-download cycles).

### Start the scheduler

```bash
pubrecsearch schedule
```

Starts the APScheduler daemon with all 14 scrapers on their cron schedules. Blocks indefinitely. Each scraper runs on its configured schedule:

| Source | Schedule | Frequency |
|--------|----------|-----------|
| OFAC SDN | `0 6 * * *` | Daily at 6 AM |
| ATF FFL | `0 8 1 * *` | Monthly on the 1st |
| FDA Debarment | `0 9 1 * *` | Monthly on the 1st |
| SAM.gov Exclusions | `0 5 * * *` | Daily at 5 AM |
| OIG LEIE | `0 10 1 * *` | Monthly on the 1st |
| FEC Contributions | `0 4 * * *` | Daily at 4 AM |
| SEC EDGAR | `0 3 * * *` | Daily at 3 AM |
| IRS Form 990 | `0 2 * * *` | Daily at 2 AM |
| CMS Open Payments | `0 1 1 1 *` | Annually on Jan 1st |
| USASpending | `0 5 * * 0` | Weekly on Sunday |
| LDA | `0 6 * * 1` | Weekly on Monday |
| FARA | `0 4 * * *` | Daily at 4 AM |
| DOJ Press | `0 7 * * *` | Daily at 7 AM |
| EPA ECHO | `0 6 * * 3` | Weekly on Wednesday |

**Deployment:** The scheduler is a blocking process. Run it in a `tmux` session or as a `systemd` service to keep it alive after SSH disconnection.

#### tmux (simple)

```bash
tmux new-session -d -s pubrecsearch 'pubrecsearch schedule'
tmux attach -t pubrecsearch   # to view logs
```

#### systemd (recommended for production)

Create `/etc/systemd/system/pubrecsearch.service`:

```ini
[Unit]
Description=PubRecSearch scraper scheduler
After=postgresql.service network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/pubrecsearch
EnvironmentFile=/path/to/pubrecsearch/.env
ExecStart=/path/to/venv/bin/pubrecsearch schedule
Restart=on-failure
RestartSec=60

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable pubrecsearch
systemctl start pubrecsearch
systemctl status pubrecsearch
journalctl -u pubrecsearch -f   # follow logs
```

---

## Running the Query API

```bash
pubrecsearch serve
# or with options:
pubrecsearch serve --host 0.0.0.0 --port 8000
```

Starts the FastAPI server at `http://127.0.0.1:8000` by default. See `docs/api.md` for endpoint documentation.

The API and the scheduler can run simultaneously in separate processes — they both connect to the same PostgreSQL database and the API is read-only.

---

## Monitoring

### Dashboard

Open `http://127.0.0.1:8000/dashboard` in a browser while the API is running. Shows per-source status, new record counts, error counts, and the current cursor for each source. Auto-refreshes every 5 minutes.

### Structured logs

All components use `structlog` with JSON output to stdout. Each log event has:
- `timestamp`: ISO 8601
- `level`: `info`, `warning`, `error`, `critical`
- `source_id`: bound to the logger for all scraper log events
- `event`: the event name (e.g., `target_skipped_unchanged`, `record_write_failed`)
- Additional context fields depending on the event

Log output can be piped to a file or log aggregator:
```bash
pubrecsearch schedule 2>&1 | tee logs/scraper.log
```

### Email alerts

If `RESEND_API_KEY` and `ALERT_EMAIL` are configured in `.env`, email alerts are sent via the Resend API for:
- **Job failure:** Sent when a scraper's `discover()` step fails, or when `errors_count > 0` and `records_processed == 0` (status = `failed`). Subject: `[PubRecSearch] FAILED: {source_id}`
- **Daily heartbeat:** Can be triggered manually via `monitoring.send_daily_heartbeat()`; not currently scheduled automatically

Alert sending is best-effort — a failure to send an alert is logged at `warning` level but never raises an exception. This ensures alert delivery failures do not kill a scraper job.

### `scrape_jobs` table

Every scrape run is logged permanently in `scrape_jobs`. Useful queries:

```sql
-- Last run for each source
SELECT DISTINCT ON (source_id)
    source_id, status, finished_at, records_new, errors_count
FROM scrape_jobs
ORDER BY source_id, started_at DESC;

-- Recent failures
SELECT source_id, started_at, status, notes
FROM scrape_jobs
WHERE status = 'failed'
ORDER BY started_at DESC
LIMIT 20;

-- Error rate over time
SELECT
    source_id,
    date_trunc('week', started_at) AS week,
    COUNT(*) FILTER (WHERE status = 'failed') AS failures,
    COUNT(*) AS total
FROM scrape_jobs
GROUP BY source_id, week
ORDER BY week DESC;
```

### `scrape_errors` table

Per-record structured errors with three severity levels:
- `warning`: One record failed; processing continued
- `error`: One target (file/URL) failed; other targets continued
- `critical`: Entire file parse failed

```sql
-- Recent critical errors
SELECT se.ts, sj.source_id, se.message, se.context
FROM scrape_errors se
JOIN scrape_jobs sj ON sj.id = se.job_id
WHERE se.level = 'critical'
  AND se.ts > now() - interval '7 days'
ORDER BY se.ts DESC;
```

---

## Historical Backfill

After initial setup, run bootstrap scripts to populate historical data. See `docs/bootstrap.md` for detailed instructions per source.

**Recommended approach for large backfills:**

1. Drop the trigram index before bulk loading millions of rows:
   ```sql
   DROP INDEX IF EXISTS individuals_trgm_idx;
   ```

2. Run bootstrap scripts (FEC, CMS, IRS 990, USASpending are the large ones)

3. Rebuild the trigram index after:
   ```sql
   CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);
   ```
   This will take 15–60 minutes depending on row count.

The ongoing incremental scrapers should be paused (don't start the scheduler) while the bulk backfill is running, to avoid contention and index invalidation.

---

## Database Maintenance

### Check table sizes

```sql
SELECT
    tablename,
    pg_size_pretty(pg_total_relation_size(tablename::regclass)) AS total_size
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY pg_total_relation_size(tablename::regclass) DESC;
```

### VACUUM / ANALYZE

After large bulk inserts, run:

```sql
VACUUM ANALYZE individuals;
VACUUM ANALYZE individual_documents;
VACUUM ANALYZE documents;
```

PostgreSQL's autovacuum will handle this automatically for ongoing small inserts, but after a bootstrap run inserting millions of rows, a manual VACUUM ANALYZE speeds up subsequent queries significantly by updating table statistics.

### Resetting a source

If you need to completely re-ingest a source from scratch:

```sql
-- 1. Find all document IDs for the source
SELECT id INTO TEMP _docs FROM documents WHERE source_id = 'fec_contributions';

-- 2. Delete individual_documents links
DELETE FROM individual_documents WHERE document_id IN (SELECT id FROM _docs);

-- 3. Optionally delete orphaned individuals (no remaining links)
DELETE FROM individuals WHERE id NOT IN (SELECT individual_id FROM individual_documents);

-- 4. Delete documents rows
DELETE FROM documents WHERE source_id = 'fec_contributions';

-- 5. Reset the scrape_state cursor and metadata
UPDATE scrape_state SET cursor = NULL, metadata = NULL WHERE source_id = 'fec_contributions';
```

This does **not** delete the raw files from R2. They will be skipped by SHA-256 dedup on next ingest. To force re-ingestion, also set `file_hash` to a dummy value or delete the documents rows (already done above).

---

## Adding a New Scraper

1. Create `src/pubrecsearch/scrapers/my_source.py` subclassing `BaseScraper`:
   ```python
   class MySourceScraper(BaseScraper):
       source_id = "my_source"
       schedule = "0 8 * * *"    # 5-field cron
       doc_type = "csv"

       def discover(self, state): ...
       def fetch(self, target): ...
       def parse(self, raw, target): ...
   ```

2. Add it to `ALL_SCRAPERS` in `src/pubrecsearch/scrapers/__init__.py`:
   ```python
   from .my_source import MySourceScraper
   ALL_SCRAPERS = [
       ...,
       MySourceScraper(),
   ]
   ```

3. Test it:
   ```bash
   pubrecsearch run my_source
   ```

4. The scheduler picks it up automatically on next start — no other configuration needed.

See `docs/pipeline.md` for the full scraper interface contract.

---

## R2 Backup Strategy

### Database backups

Periodic PostgreSQL dumps should be uploaded to R2:

```bash
pg_dump pubrecsearch | gzip > pubrecsearch_$(date +%Y%m%d).sql.gz
aws s3 cp pubrecsearch_$(date +%Y%m%d).sql.gz \
    s3://pubrecsearch-raw/backups/ \
    --endpoint-url $R2_ENDPOINT
```

**Assumption:** The `backups/` prefix in R2 is reserved for database dumps. Raw source files live under `raw/`.

### Recovery

To rebuild a PostgreSQL database from a dump:

```bash
createdb pubrecsearch
psql pubrecsearch < pubrecsearch_20250427.sql
```

Alternatively, the database can be rebuilt from scratch by running `init-db` and then re-running all bootstrap scripts — R2 retains all raw source files, and SHA-256 dedup means nothing is re-downloaded.

---

## Environment Variables Reference

All variables are defined in `src/pubrecsearch/config.py` via Pydantic `BaseSettings`. They can be set in `.env` or as environment variables.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | Yes | `postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch` | PostgreSQL connection string |
| `R2_ENDPOINT` | Yes | `""` | Cloudflare R2 endpoint URL |
| `R2_ACCESS_KEY` | Yes | `""` | R2 access key ID |
| `R2_SECRET_KEY` | Yes | `""` | R2 secret access key |
| `R2_BUCKET` | Yes | `pubrecsearch-raw` | R2 bucket name |
| `RESEND_API_KEY` | No | `""` | Resend API key for email alerts (blank = alerts disabled) |
| `ALERT_EMAIL` | No | `""` | Recipient email for alerts |
| `SAM_API_KEY` | For SAM scraper | `""` | SAM.gov free API key |
| `EDGAR_USER_AGENT` | For EDGAR | `PubRecSearch contact@example.com` | SEC requires real contact info here |
| `LDA_API_KEY` | For LDA scraper | `""` | LDA Senate API key |
| `LOG_LEVEL` | No | `INFO` | Log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

**Assumptions:**
- R2 credentials must have `GetObject`, `PutObject`, and `GeneratePresignedUrl` permissions
- `DATABASE_URL` must be a psycopg2-compatible connection string (not asyncpg)
- Missing API keys cause per-source failures logged at error level; other scrapers continue

---

## Troubleshooting

**Scraper returns `status: failed` every run:**
1. Check `scrape_errors` for the job ID
2. Check the `notes` field in `scrape_jobs` for a summary
3. Run `pubrecsearch run <source_id>` to see logs in real time

**Search returns no results:**
Check that `individuals_trgm_idx` exists:
```sql
SELECT indexname FROM pg_indexes WHERE tablename = 'individuals';
```
If missing, recreate it:
```sql
CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);
```

**R2 upload failures:**
Verify credentials and bucket name. Test with:
```python
from pubrecsearch.r2 import upload
upload("test/ping.txt", b"ping", "text/plain")
```

**PostgreSQL "too many connections" error:**
The pipeline opens one connection per scraper run. With all 14 scrapers running on overlapping schedules, connection pool pressure can occur. Increase `max_connections` in `postgresql.conf` or add a connection pooler (PgBouncer).

**CMS bootstrap fails with "no space left on device":**
The CMS file is ~8.91 GB uncompressed. The streaming path writes to `tempfile.mkdtemp()`, which uses the system temp directory (`/tmp` on Linux). Ensure at least 10 GB free on the filesystem containing `/tmp`.

**"normalize_name returned empty string" errors after a code change:**
If `normalize_name()` in `src/pubrecsearch/normalize.py` is modified, all existing `name_norm` values in the database become inconsistent. To fix: re-run all bootstrap scripts to regenerate `name_norm` values. Never change the normalization function without a full re-ingest. See `docs/database.md` for details.
