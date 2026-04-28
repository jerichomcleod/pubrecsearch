# Historical Bootstrap Scripts

Bootstrap scripts are one-time (or rarely re-run) operations that backfill historical data from 2015 to the present. They live in `scripts/bootstrap/` and are run manually, not by the scheduler.

All scripts:
- Add `src/` to `sys.path` so they can import the `pubrecsearch` package directly without installation
- Use the same `db`, `r2`, normalization, and scraper internals as the production pipeline
- Checkpoint progress in `scrape_state.metadata` so runs can be safely interrupted and resumed
- Use the `--force` flag to re-process already-completed units (cycles, years, quarters, etc.)

**Prerequisite:** Run `pubrecsearch init-db` and configure `.env` before running any bootstrap script.

---

## When to Run Bootstrap Scripts

Bootstrap scripts are needed in one scenario: **the first setup of a new installation**. Each source's ongoing scraper only handles new data going forward. The bootstrap scripts load the historical record back to 2015.

Do not run bootstrap scripts on an already-populated database unless you intend to re-process data (use `--force` carefully).

**Recommended run order** (approximate, based on data size and dependencies):

1. `sam_exclusions_import.py` — one-shot, requires manual CSV download first
2. `fec_bootstrap.py` — largest; run before other scripts to get indexes dropped and rebuilt
3. `cms_bootstrap.py` — large files; uses streaming path
4. `usaspending_bootstrap.py` — large; async job creation
5. `irs990_bootstrap.py` — many small files; requires patience
6. `edgar_bootstrap.py` — quarterly, fast per-quarter
7. `lda_bootstrap.py` — paginated API; rate-limited
8. `doj_bootstrap.py` — web scraping; rate-limited
9. `epa_bootstrap.py` — API; rate-limited

Phase 1 sources (OFAC SDN, ATF FFL, FDA Debarment, OIG LEIE) have no bootstrap scripts — their source files are full replacements, so running `pubrecsearch run <source_id>` once is sufficient to load all current data.

---

## `fec_bootstrap.py`

**Source:** FEC individual campaign contributions  
**Historical data:** 2016 election cycle (covers 2015–2016) through current cycle  
**File size:** 300–700 MB compressed per cycle, 2–5 GB uncompressed  
**Estimated runtime:** Several hours per cycle (network + bulk COPY)

```bash
# All in-scope cycles (2016–2024)
python scripts/bootstrap/fec_bootstrap.py --from-cycle 2016 --to-cycle 2024

# Single cycle
python scripts/bootstrap/fec_bootstrap.py --cycle 2020

# Re-process a cycle (e.g., if parsing was improved)
python scripts/bootstrap/fec_bootstrap.py --cycle 2020 --force
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--cycle` | — | Single cycle year (must be even: 2016, 2018, 2020, ...) |
| `--from-cycle` | 2016 | First cycle to process |
| `--to-cycle` | 2024 | Last cycle to process |
| `--force` | false | Re-process even if already marked done |

**How it works:**

FEC uses 2-year election cycles ending in even years. The file for cycle year `N` covers contributions from `N-1` through `N`. Cycle 2016 = contributions filed for the 2015–2016 cycle.

The script downloads `https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com/bulk-downloads/{year}/indiv{yy}.zip` for each cycle year. These are the FEC bulk download files — large fixed-width text files requiring custom parsing.

Checkpointing: Completed cycles are stored in `scrape_state.metadata["bootstrap_cycles"]` for `source_id="fec_contributions"`. If the script is interrupted mid-cycle, the entire cycle will be re-attempted on next run (the file will already be in R2 and will skip the download via SHA-256 dedup, but parsing restarts).

**Index maintenance note:** With 50–100M rows per cycle, the trigram index should be dropped before the first bulk load and rebuilt after (see database.md). The script does not do this automatically.

---

## `edgar_bootstrap.py`

**Source:** SEC EDGAR Form 4, Form 3, SC 13G/A insider ownership filings  
**Historical data:** Q1 2015 through current quarter  
**File size:** 5–15 MB compressed per quarterly index  
**Estimated runtime:** 1–3 hours total (fast downloads, moderate parsing)

```bash
# All quarters 2015–2024
python scripts/bootstrap/edgar_bootstrap.py --from-year 2015 --to-year 2024

# Single year (processes all 4 quarters)
python scripts/bootstrap/edgar_bootstrap.py --year 2020

# Single quarter
python scripts/bootstrap/edgar_bootstrap.py --year 2020 --quarter 3

# Re-process
python scripts/bootstrap/edgar_bootstrap.py --year 2020 --force
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single year (processes all 4 quarters unless `--quarter` specified) |
| `--quarter` | — | Single quarter (1–4); only used with `--year` |
| `--from-year` | 2015 | First year to process |
| `--to-year` | current year | Last year to process |
| `--force` | false | Re-process completed quarters |

**How it works:**

EDGAR publishes quarterly full-text index files at `https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{q}/company.gz`. The script downloads each file, filters it to Form 4, Form 3, SC 13G, and SC 13G/A entries, and extracts the filer names as `sec_insider` or `sec_beneficial_owner`.

The SEC requires a `User-Agent` header identifying the requester (see `config.py → edgar_user_agent`). Missing or invalid User-Agent results in HTTP 403.

Checkpointing: Completed quarters are stored in `scrape_state.metadata["bootstrap_quarters"]` for `source_id="sec_edgar"`.

---

## `irs990_bootstrap.py`

**Source:** IRS Form 990 electronic filings (nonprofit officers/directors)  
**Historical data:** 2015 through most recent available year  
**Volume:** ~150K–200K filings per year, each requiring one HTTP request  
**Estimated runtime:** Days for full backfill (rate-limited to IRS S3)

```bash
# All years 2015–2023
python scripts/bootstrap/irs990_bootstrap.py --from-year 2015 --to-year 2023

# Single year
python scripts/bootstrap/irs990_bootstrap.py --year 2020

# Process in batches (resume safely; processes up to N filings then stops)
python scripts/bootstrap/irs990_bootstrap.py --year 2020 --limit 10000

# Continue from last checkpoint
python scripts/bootstrap/irs990_bootstrap.py --year 2020 --resume
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single year |
| `--from-year` | 2015 | First year |
| `--to-year` | — | Last year |
| `--limit` | 0 (unlimited) | Stop after processing N filings in this run |
| `--resume` | false | Skip filings already in `bootstrap_processed` checkpoint |
| `--force` | false | Re-process entire year, ignoring checkpoint |

**How it works:**

The IRS publishes an annual JSON index at `https://apps.irs.gov/pub/efiles/eo/eo{year}.json` listing all available 990 XML files for that year. The script fetches the index, then downloads each individual XML from IRS S3 (`https://s3.amazonaws.com/irs-form-990/{object_id}_public.xml`) and parses `<Officer>`, `<Director>`, and `<Trustee>` elements.

**Warning:** The full backfill is extremely time-consuming. Use `--limit` to run in chunks across multiple sessions. Each year with 150K filings at ~0.3 req/sec takes roughly 5+ hours.

Checkpointing: Processed filing URLs are stored in `scrape_state.metadata["bootstrap_processed"]` for `source_id="irs_990"`. Because this list can grow very large, completed years are also tracked separately so the entire year can be skipped quickly.

---

## `cms_bootstrap.py`

**Source:** CMS Open Payments (physician payment records)  
**Historical data:** 2015 through most recent program year  
**File size:** 2–4 GB compressed per year, up to ~8.91 GB uncompressed  
**Estimated runtime:** 30–90 minutes per year (streaming download + Polars lazy scan)

```bash
# All years 2015–2023
python scripts/bootstrap/cms_bootstrap.py --from-year 2015 --to-year 2023

# Single year
python scripts/bootstrap/cms_bootstrap.py --year 2020
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single program year |
| `--from-year` | 2015 | First year |
| `--to-year` | current year | Last year |
| `--force` | false | Re-process completed years |

**How it works:**

CMS publishes annual ZIP files containing General Payment data CSVs at `https://download.cms.gov/openpayments/PGYR{YY}_P{date}.ZIP`. The bootstrap script uses a hardcoded URL map (`_YEAR_URLS`) for known stable URLs. The exact date suffix in the filename changes when CMS refreshes the data; the map should be verified annually.

Because these files exceed available RAM, the script uses the **streaming path**: downloads to a temp file on disk, hashes and uploads to R2 incrementally, then parses via Polars lazy scanning. The temp file is cleaned up in a `finally` block.

Checkpointing: Completed years are stored in `scrape_state.metadata["bootstrap_years"]` for `source_id="cms_open_payments"`. Interrupted mid-download runs will restart the full-year download (temp files are not preserved across runs).

**Disk space requirement:** Ensure at least 10 GB of free space in the system temp directory before running.

---

## `usaspending_bootstrap.py`

**Source:** USASpending.gov federal award recipients  
**Historical data:** FY2015 (Oct 2014 – Sep 2015) through current fiscal year  
**Estimated runtime:** 5–20 minutes per fiscal year (API job creation + polling)

```bash
# All fiscal years 2015–2024
python scripts/bootstrap/usaspending_bootstrap.py --from-year 2015 --to-year 2024

# Single fiscal year
python scripts/bootstrap/usaspending_bootstrap.py --year 2020
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single fiscal year |
| `--from-year` | 2015 | First fiscal year |
| `--to-year` | current FY | Last fiscal year |
| `--force` | false | Re-process completed years |

**How it works:**

USASpending.gov uses an asynchronous bulk download API. The script:
1. POSTs a download job request to `https://api.usaspending.gov/api/v2/bulk_download/awards/` specifying date range, `recipient_type: "individual"`, and award types
2. Polls the job status URL every 30 seconds until the job completes (status: `finished`)
3. Downloads the resulting ZIP file from the presigned S3 URL provided in the job response
4. Extracts and parses the individual recipient CSV within the ZIP

FY2015 = October 1, 2014 through September 30, 2015. The `_fy_dates(fy)` helper computes these dates correctly.

Checkpointing: Completed fiscal years are stored in `scrape_state.metadata["bootstrap_years"]` for `source_id="usaspending"`.

---

## `lda_bootstrap.py`

**Source:** LDA Lobbying Disclosure Act filings  
**Historical data:** 2015 through current year  
**Volume:** Hundreds of thousands of filings per year  
**Estimated runtime:** Several hours per year (API rate-limited to 120 req/min)

```bash
# All years 2015 to present
python scripts/bootstrap/lda_bootstrap.py

# Specific range
python scripts/bootstrap/lda_bootstrap.py --from-year 2015 --to-year 2020

# Single year
python scripts/bootstrap/lda_bootstrap.py --year 2022

# Re-process a year
python scripts/bootstrap/lda_bootstrap.py --year 2023 --force
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single year |
| `--from-year` | 2015 | First year |
| `--to-year` | current year | Last year |
| `--force` | false | Re-process completed years |

**How it works:**

The script calls `fetch_year_filings(year)` from `lda.py`, which paginates the LDA REST API with `received_dt__gte={year}-01-01&received_dt__lte={year}-12-31&page_size=25`. After fetching all pages, the complete list of filings is serialized to JSON, uploaded to R2 as `raw/lda/{year}/lda_filings_{year}.json`, then passed to `filings_to_rows()` to extract lobbyist rows for bulk insertion.

**Requirement:** `LDA_API_KEY` must be set in `.env`. Free registration at `https://lda.senate.gov/api/`.

Checkpointing: Completed years are stored in `scrape_state.metadata["bootstrap_completed_years"]` for `source_id="lda"`. Rows are inserted in 10,000-row batches via `bulk_insert_individuals()`.

---

## `doj_bootstrap.py`

**Source:** DOJ press releases (defendant name extraction)  
**Historical data:** 2015 to present (~40,000+ press releases)  
**Estimated runtime:** Several hours for full pass without `--titles-only`; 30–60 minutes with `--titles-only`

```bash
# Full backfill (fetches each press release page individually — slow)
python scripts/bootstrap/doj_bootstrap.py

# Titles-only mode (fast first pass; lower extraction accuracy)
python scripts/bootstrap/doj_bootstrap.py --titles-only

# Date-constrained run
python scripts/bootstrap/doj_bootstrap.py --from-date 2020-01-01 --to-date 2022-12-31

# Limit number of listing pages (each page has ~20 releases)
python scripts/bootstrap/doj_bootstrap.py --max-pages 500 --titles-only
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--from-date` | 2015-01-01 | Earliest press release date to include |
| `--to-date` | today | Latest date to include |
| `--max-pages` | 5000 | Maximum listing pages to paginate (each has ~20 releases) |
| `--titles-only` | false | Extract names from listing page titles only; skip individual HTML fetches |
| `--force` | false | Re-process already-recorded URLs |

**How it works:**

The DOJ press release listing at `https://www.justice.gov/news/press-releases?page=N` is paginated HTML. The script walks pages from newest to oldest, calling `fetch_listing_page(page)` for each. For each discovered press release URL:

- **Without `--titles-only`:** Fetches the full HTML of each release page (1 req/release at ~1 req/sec), extracts title + body text, applies both `_TITLE_PATTERNS` and `_BODY_PATTERNS` for name extraction.
- **With `--titles-only`:** Uses only the title text from the listing page. No per-page HTTP requests. Faster but misses names only mentioned in the body. A synthetic JSON document is stored in R2 to record which listing pages were processed.

Rate limit: `_REQ_DELAY = 1.1` seconds between requests enforced by the scraper.

Checkpointing: Processed press release URLs are accumulated in `scrape_state.metadata["bootstrap_processed_urls"]`. Because this set can grow large for a full historical run, the checkpoint is written every 100 pages.

**Recommendation:** Run with `--titles-only` first for a fast initial pass, then optionally re-run without that flag to improve extraction on titles that didn't yield names.

---

## `epa_bootstrap.py`

**Source:** EPA ECHO enforcement cases with individual defendants  
**Historical data:** 2015 to present  
**Volume:** Estimated 5,000–15,000 cases with individual defendants since 2015  
**Estimated runtime:** Several hours (API rate-limited to 2 req/sec)

```bash
# All years 2015 to present
python scripts/bootstrap/epa_bootstrap.py

# Specific range
python scripts/bootstrap/epa_bootstrap.py --from-year 2015 --to-year 2020

# Single year
python scripts/bootstrap/epa_bootstrap.py --year 2022

# Re-process a year
python scripts/bootstrap/epa_bootstrap.py --year 2022 --force
```

**Arguments:**

| Flag | Default | Description |
|------|---------|-------------|
| `--year` | — | Single year |
| `--from-year` | 2015 | First year |
| `--to-year` | current year | Last year |
| `--force` | false | Re-process completed years |

**How it works:**

For each year, the script calls `fetch_case_ids_for_year(year)`, which queries the ECHO case API with `p_defendant_type=I` and `p_activity_date_begin={year}-01-01&p_activity_date_end={year}-12-31`. It then fetches full case detail for each case ID via `_fetch_case_detail()`. The combined case data is serialized to JSON, uploaded to R2 as `raw/epa_echo/{year}/epa_cases_{year}.json`, and parsed via `parse_cases()`.

Checkpointing: Completed years are stored in `scrape_state.metadata["bootstrap_completed_years"]` for `source_id="epa_echo"`.

**Rate limit:** 0.5 seconds between requests to ECHO API. At 5,000+ case detail requests per year, expect 45+ minutes per year at this rate.

---

## `sam_exclusions_import.py`

**Source:** SAM.gov Exclusions public extract  
**Type:** One-shot import of a pre-downloaded CSV  
**Use case:** The SAM.gov API is rate-limited to 10 requests/day. On first setup, the full extract CSV must be downloaded manually from SAM.gov and then imported with this script.

```bash
python scripts/bootstrap/sam_exclusions_import.py \
    --file tmp/SAM_Exclusions_Public_Extract_V2_26113.CSV

# Dry run (validates the file and shows what would be inserted without writing to DB)
python scripts/bootstrap/sam_exclusions_import.py \
    --file tmp/SAM_Exclusions_Public_Extract_V2_26113.CSV \
    --dry-run
```

**Arguments:**

| Flag | Required | Description |
|------|----------|-------------|
| `--file PATH` | Yes | Path to the locally downloaded SAM exclusions CSV |
| `--dry-run` | No | Validate and count records without writing to DB or R2 |

**How to obtain the CSV:**

1. Go to `https://sam.gov/data-services/Exclusions/`
2. Download the "SAM Exclusions Public Extract" CSV (the full extract, not a delta)
3. The filename will be something like `SAM_Exclusions_Public_Extract_V2_YYYYJJJ.CSV`

**What the script does:**

1. Reads the CSV from disk
2. Computes SHA-256; if the hash already exists in `documents`, skips (already imported)
3. Uploads the raw CSV to R2 at `raw/sam_exclusions/{today}/{filename}`
4. Calls `SamExclusionsScraper.parse()` to filter individual exclusions and build records
5. Bulk-inserts in 10,000-row batches via `bulk_insert_individuals()`
6. Advances the `scrape_state` cursor to today so the daily scheduled scraper picks up from here

**After running this script**, the automated `SamExclusionsScraper` (scheduled daily) will handle ongoing updates using the SAM.gov async extract API to download delta files.

---

## Checkpoint Storage

All bootstrap scripts store progress in the `scrape_state` table under the `metadata` JSONB column for their respective `source_id`. The general pattern:

```python
state = db.get_state(source_id)
meta = state.get("metadata") or {}
completed = meta.get("bootstrap_completed_years", [])  # or bootstrap_cycles, bootstrap_quarters, etc.

if year in completed and not force:
    return  # already done

# ... do work ...

completed.append(year)
meta["bootstrap_completed_years"] = sorted(set(completed))
db.set_state(source_id, cursor=state.get("cursor"), metadata=meta)
```

**Consequence:** The `metadata` column is shared between bootstrap checkpoints and the ongoing scraper's state. The bootstrap scripts read the full metadata dict and write it back with only their key modified, preserving any other keys the ongoing scraper may have written.

**Cursor vs metadata:** Bootstraps generally do not update the `cursor` column. The cursor is owned by the ongoing incremental scraper and is only advanced on its successful runs. Bootstrap scripts use `metadata` to track their own progress independently.

---

## Re-running Bootstrap Scripts

Use `--force` to re-process a unit (cycle, year, quarter) that has already been marked done. This is safe because:
- The pipeline's SHA-256 deduplication will skip files already in R2
- `individual_documents` inserts use `ON CONFLICT DO NOTHING`, so duplicates aren't created
- `individuals` inserts use `ON CONFLICT DO NOTHING` on `name_norm`

The main cost of a forced re-run is re-doing the COPY/bulk-insert work, which is idempotent but not free.
