# The Scrape Pipeline

This document covers the full lifecycle of a scrape job: how the runner orchestrates a scraper, how cursors track progress, how errors are handled, and how the APScheduler runs everything on cron schedules.

---

## The Scraper Interface

Every data source is a subclass of `BaseScraper` (`src/pubrecsearch/base_scraper.py`). The interface has three required methods and two optional ones:

```
BaseScraper
├── source_id   str          snake_case; matches scrape_state.source_id
├── schedule    str          5-field cron expression
├── doc_type    str          csv / xml / json / zip / html (used for R2 content-type)
│
├── discover(state: dict) -> list[DownloadTarget]     [REQUIRED]
├── fetch(target) -> bytes                            [REQUIRED]
├── parse(raw, target) -> list[ParsedRecord]          [REQUIRED]
│
├── fetch_to_file(target) -> Path                     [OPTIONAL — large files]
└── parse_to_db(raw|None, target, doc_id) -> (int,int) [OPTIONAL — bulk COPY]
```

### `discover(state)`

Returns the list of `DownloadTarget` objects to process this run. The `state` dict is the `scrape_state` row for this source (cursor, metadata, last_run_at, last_success_at).

The contract: **only return targets that have not been successfully processed**. Use the cursor or metadata to skip already-done work. If nothing is new, return an empty list.

**Assumption:** `discover()` is allowed to make HTTP calls (some scrapers check a metadata endpoint or RSS feed). If `discover()` raises, the entire job is marked `failed` and no targets are processed.

### `fetch(target)`

Downloads raw bytes for one target. Should raise on HTTP error. Retry logic (tenacity decorators) belongs inside `fetch()`, not in the runner.

### `parse(raw, target)`

Extracts structured records from raw bytes. Returns a list of `ParsedRecord` objects, each containing one or more `ParsedIndividual` objects. Should raise on unrecoverable file-level errors; for per-record errors, log them and continue returning partial results.

### `fetch_to_file(target)` — optional

For files too large to hold in RAM (≥ ~1 GB). Streams the download to a temp file on disk and returns the `Path`. The runner detects this method's presence and switches to the streaming path automatically — see below.

### `parse_to_db(raw, target, doc_id)` — optional

For large bulk sources (FEC, IRS 990, CMS, etc.). Directly inserts rows into PostgreSQL via `COPY`, bypassing the per-row `_write_record()` loop. Returns `(records_processed, records_new)`. When `fetch_to_file()` was used, `raw` is `None` and the file path is in `target.metadata["_local_path"]`.

---

## The Runner Lifecycle

`ScraperRunner.run()` (`src/pubrecsearch/runner.py`) is the single entry point for one full scrape cycle. Here is the exact execution order:

```
1. db.get_state(source_id)               → load cursor and metadata
2. db.start_job(source_id)              → INSERT scrape_jobs row, status="running"
3. scraper.discover(state)              → get list of DownloadTarget objects
   (if raises → db.finish_job("failed"), send email alert, return)

For each target:
4.  _fetch_and_store_document(target)
    a. [streaming path if fetch_to_file present]
       scraper.fetch_to_file(target)    → temp file on disk
       _hash_file(path)                 → SHA-256 (8 MB chunks, no full load)
    b. [standard path]
       scraper.fetch(target)            → bytes in memory
       sha256(raw)                      → SHA-256

    c. db.document_exists(file_hash)    → if True: raise _SkipTarget (skip silently)
    d. r2.upload(key, ...)              → upload to R2 (MUST succeed before DB write)
    e. db.insert_document(...)          → insert documents row, get doc_id

5.  [if parse_to_db present]
       scraper.parse_to_db(raw, target, doc_id)  → bulk COPY
       (temp file cleaned up in finally block)
    [else]
       scraper.parse(raw, target)       → list[ParsedRecord]
       for each record: _write_record(record, doc_id)
         → db.insert_individual(name, name_norm)
         → db.insert_individual_document(...)

6. last_good_cursor = target.metadata.get("cursor_after")

7. Final status:
   - "success"  → 0 errors
   - "partial"  → errors > 0 AND records_processed > 0
   - "failed"   → errors > 0 AND records_processed == 0

8. db.finish_job(job_id, status, records_processed, records_new, errors_count)

9. [if success only] db.set_state(source_id, cursor=last_good_cursor)

10. [if failed] send_job_failure_alert(...)
```

### Critical invariant: R2 before DB

Step 4d (R2 upload) must succeed before step 4e (database insert). This ensures every `documents` row has a corresponding R2 object. If the upload fails after retries, the target is marked as an error and no DB row is written.

### Critical invariant: cursor advanced only on success

Step 9 only runs if `final_status == "success"`. If any target fails, the cursor is not advanced. On the next run, `discover()` will re-check the same time range and re-process any targets that failed. This is a key design choice: **retry on the next scheduled run automatically, without manual intervention**.

**Implication:** A `partial` job leaves the cursor unchanged. The same targets will be re-discovered on the next run. For idempotent targets (OFAC full-replacement, FARA daily snapshots), this is safe — the SHA-256 dedup step will skip already-processed files. For cursor-advancing targets (LDA, EDGAR), re-processing could produce duplicate `individual_documents` rows, but these are protected by `ON CONFLICT DO NOTHING` in the insert.

---

## The DownloadTarget

```python
@dataclass
class DownloadTarget:
    url: str          # URL to fetch
    source_id: str    # source identifier
    period: str       # used in R2 key: "2025-04", "2024-Q1", "2025-04-27"
    doc_type: str     # file type: "csv", "xml", "json", "zip", "html"
    filename: str     # used in R2 key: "sdn_xml.zip", "form.idx.gz"
    headers: dict     # extra HTTP headers (default: empty)
    metadata: dict    # scraper-specific state passed through the pipeline
```

The `metadata` dict is the primary communication channel between `discover()` and `parse_to_db()` / `parse()`. Scrapers store everything they need here: the `cursor_after` value to advance the cursor on success, API response metadata, extracted file paths, etc.

**`cursor_after`:** Any scraper that wants the cursor advanced on success must set `target.metadata["cursor_after"]` to the new cursor value. The runner reads this at step 6 and passes it to `db.set_state()` at step 9.

**`_local_path`:** Set by the runner (not the scraper) at step 4a. The runner writes the temp file path here so `parse_to_db()` can find it. The scraper should read it as `target.metadata.get("_local_path")`.

---

## R2 Key Convention

```
raw/{source_id}/{period}/{filename}

Examples:
  raw/ofac_sdn/2025-04-27/sdn_xml.zip
  raw/fec_contributions/2024/indiv24.zip
  raw/irs_990/2024/2024_TEOS_XML_01A.zip
  raw/doj_press/2025-04/doj_two-men-sentenced.html
```

The `period` component is flexible per source (see individual source docs) and is used only for organizational grouping in R2 — it is not parsed back anywhere.

---

## SHA-256 File-Level Deduplication

Before any parsing or DB write, the runner checks whether a document with the same SHA-256 hash already exists in the `documents` table. If it does, the target is silently skipped (logged as `target_skipped_unchanged`).

**What this means:** If the content of a file hasn't changed, no work is done. This is the primary mechanism for:
- OFAC SDN (daily replacement): only re-parsed if the list actually changed
- FARA (daily replacement): only re-parsed if the CSV changed
- ATF FFL (monthly replacement): only re-parsed if the CSV changed

**What this does not protect against:** Two different runs downloading the same file via different URLs will both get unique `r2_key` values but the same hash. The second `db.document_exists()` check will return True and skip, but the `r2.upload()` at step 4d will have already run (wasting the upload). This is a minor inefficiency, not a correctness issue.

---

## Incremental Cursors

Each source has a row in `scrape_state` with a `cursor` column. The cursor is a source-specific string that marks the most recent successfully processed point. Its format and meaning vary:

| Source | Cursor format | Meaning |
|--------|---------------|---------|
| OFAC SDN | ISO date (YYYY-MM-DD) | Date of last successful run |
| ATF FFL | "YYMM" | Last successfully downloaded year/month |
| FEC | ISO date | Date of last run |
| SEC EDGAR | ISO date | Last daily index file date |
| IRS 990 | Batch ID (e.g. "2025_TEOS_XML_06A") | Last batch processed |
| CMS | Program year (e.g. "2024") | Last program year downloaded |
| SAM.gov | ISO date | Date of last successful import |
| LDA | ISO date | Date of last filing received |
| FARA | ISO date | Date of last successful run |
| DOJ Press | ISO datetime | pubDate of last RSS item processed |
| EPA ECHO | ISO date | Last case activity date checked |
| USASpending | ISO date | Last download date |

**`scrape_state` also has a `metadata` JSONB column** for structured state that doesn't fit in a single string cursor — for example, the IRS 990 bootstrap tracks which batches have been processed as `{"bootstrap_processed_batches": [...]}`.

---

## Error Handling Policy

The runner implements a three-level error classification:

### Level 1: `discover()` failure
If `discover()` raises any exception, the job is immediately marked `failed`. No targets are processed. An email alert is sent.

### Level 2: Target-level failure
If fetching, uploading, or parsing a target raises an exception:
- The error is logged at `error` level with full traceback
- A row is inserted into `scrape_errors` (level="error")
- `errors_count` increments
- **Processing continues with the next target**
- The cursor is not advanced for the failed target

### Level 3: Record-level failure
If writing a single `ParsedRecord` (or one of its `ParsedIndividual` rows) raises an exception during the per-row path:
- The error is logged at `warning` level
- A row is inserted into `scrape_errors` (level="warning")
- `errors_count` increments
- **Processing continues with the next record**

### Final status

```python
if errors_count == 0:
    final_status = "success"
elif records_processed > 0:
    final_status = "partial"
else:
    final_status = "failed"
```

A `failed` status triggers an email alert. A `partial` status does not — it is expected behavior when some records in a file fail to parse.

**Important:** The cursor is only advanced on `"success"`. A `partial` or `failed` run leaves the cursor at the previous value. On the next scheduled run, `discover()` will re-check the same window and will find the same targets. The SHA-256 dedup prevents re-processing successfully-stored files; failed files will be retried.

---

## APScheduler Integration

`build_scheduler(scrapers)` in `runner.py` creates a `BlockingScheduler` with one job per scraper. Each job is:

```python
scheduler.add_job(
    runner.run,
    trigger=CronTrigger(minute, hour, day, month, day_of_week),
    id=scraper.source_id,
    coalesce=True,      # if machine was asleep, run only once (not N missed runs)
    max_instances=1,    # no two jobs for the same source can run simultaneously
    misfire_grace_time=3600,  # if trigger fires while job is still running, tolerate up to 1h
)
```

**`coalesce=True`:** If the machine is turned off at the scheduled time, APScheduler will run the job once when it next starts up (not once per missed firing). This prevents a cascade of catch-up runs after a reboot.

**`max_instances=1`:** If a long-running job (e.g., FEC bulk download) hasn't finished when the next cron trigger fires, the new trigger is discarded.

**`misfire_grace_time=3600`:** A job that was supposed to fire at 2:00 AM but didn't start until 2:59 AM (because the machine was busy) will still run. If it missed by more than 1 hour, it's discarded.

The scheduler runs in the same process as the main thread (`BlockingScheduler`). Run it in a `tmux` session or as a `systemd` service to keep it running after SSH disconnection.

---

## The `parse_to_db` Bulk COPY Pattern

For sources with millions of records, the per-row `_write_record()` approach is too slow. Sources like FEC (50–100M rows/cycle) and IRS 990 use `parse_to_db()` which bypasses per-row inserts entirely.

The flow inside `bulk_insert_individuals()` in `db.py`:

```sql
-- 1. COPY raw data into a temp staging table
CREATE TEMP TABLE _bulk_staging (
    name TEXT, name_norm TEXT, excerpt TEXT, identifiers TEXT
) ON COMMIT DROP;

COPY _bulk_staging FROM STDIN;   -- TSV via Python StringIO

-- 2. Single CTE: insert distinct individuals AND link them to the document
WITH new_individuals AS (
    INSERT INTO individuals (name, name_norm)
    SELECT DISTINCT ON (name_norm) name, name_norm
    FROM _bulk_staging
    ORDER BY name_norm
    RETURNING id, name_norm
)
INSERT INTO individual_documents (individual_id, document_id, relationship, excerpt, identifiers)
SELECT
    ni.id,
    %(doc_id)s,
    %(rel)s,
    s.excerpt,
    s.identifiers::jsonb
FROM new_individuals ni
JOIN (
    SELECT DISTINCT ON (name_norm) name_norm, excerpt, identifiers
    FROM _bulk_staging
    ORDER BY name_norm
) s ON s.name_norm = ni.name_norm
ON CONFLICT DO NOTHING;
```

**Key properties:**
- The `DISTINCT ON (name_norm)` deduplicates within a single batch — one individual row per unique normalized name
- The CTE runs as a single round-trip — no intermediate commits
- `ON CONFLICT DO NOTHING` on `individual_documents` is safe because the PK is `(individual_id, document_id, relationship)` — re-running a batch won't create duplicates
- The staging table is dropped at transaction commit (`ON COMMIT DROP`)

**What this does not do:** It does not deduplicate across different document IDs. If the same person appears in two different FEC ZIP files (two different election cycles), they will have two `individuals` rows with the same `name_norm`.

---

## The Streaming Path (`fetch_to_file`)

For files where the uncompressed size exceeds available RAM:

```
scraper.fetch_to_file(target)
  → writes to /tmp/{uuid}/filename
  → returns Path

runner._hash_file(path)
  → reads 8 MB at a time
  → returns SHA-256 hex string

db.document_exists(hash)
  → if True: delete temp file, raise _SkipTarget

r2.upload_fileobj(key, open(path, "rb"), content_type)
  → multipart upload: threshold=100MB, chunk=64MB, concurrency=4

scraper.parse_to_db(raw=None, target, doc_id)
  → scraper reads from target.metadata["_local_path"]
  → uses Polars lazy scanning: scan_csv(...).collect(engine="streaming")

os.unlink(local_path)    ← in finally block; always cleaned up
```

Currently only CMS Open Payments uses this path. Any scraper that defines `fetch_to_file()` will automatically use it — the runner detects the method via `hasattr(self.scraper, "fetch_to_file")`.

**Assumption:** The machine has enough free disk space for the largest file being processed (CMS is ~8.91 GB). Temp files are always in the system temp directory (`tempfile.mkdtemp()`). They are cleaned up in a `finally` block even if parsing fails.
