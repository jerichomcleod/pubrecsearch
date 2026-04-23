# PubRecSearch — Master Implementation Plan

PubRecSearch is a batch scraping pipeline, datalake, and search interface for U.S. federal public records. It continuously ingests records from 14 federal sources, extracts named individuals, stores raw source files in Cloudflare R2, and provides a queryable index linking individuals to their source documents.

The system is **not** a real-time service. It is a scheduled batch pipeline with a read interface. Architecture decisions favor operational simplicity and cost over latency and throughput.

---

## 1. Data Scale Analysis

### Per-Source Scale Estimates

| Source | Total Records (Current) | New Records / Year | Raw File Size / Year | DB Rows / Year | Notes |
|---|---|---|---|---|---|
| **FEC Individual Contributions** | ~300M+ (full history 1979–) | ~50–100M | ~4–5 GB/cycle compressed | ~50–100M | Dominant row-count source |
| **SEC EDGAR** | Millions of filings (1993–) | ~500K individual-relevant | ~10–15 GB/year | ~500K–1M | Scoped to Form 4/3/SC13G only |
| **USASpending.gov** | FY2001–present | Millions of award actions/year | ~5–15 GB/year compressed | ~1–2M | Filter aggressively to individuals at parse time |
| **IRS Form 990** | 3M+ returns; 9.6M+ documents | ~150K–200K filings | ~5 GB/year compressed | ~2–4M | Officer/director fields are extraction target |
| **DOJ Press Releases** | Years of archives | ~3K–8K releases | ~0.5–1 GB/year | ~10K–30K | Regex extraction; store confidence level |
| **OFAC SDN List** | ~15,000 total entries | Hundreds/year | ~20 MB (full XML, daily replacement) | ~5K individuals | Small; full list replaced each update |
| **ATF FFL** | ~128,690 active (FY2024) | ~44K issued/year | ~5–10 MB/month | ~130K (stable) | Monthly CSV; sole proprietors targeted |
| **LDA Lobbying** | 726K+ cumulative (2018–) | ~55K filings/year | ~50–100 MB/year | ~100K–200K | Quarterly LD-1/LD-2/LD-203 reports |
| **FARA** | ~500 active registrants | ~1K–2K filings/year | ~10–20 MB/year | ~2K–5K | Small; bulk XML available |
| **CMS Open Payments** | 84M cumulative (2013–2024) | ~16M/year | ~3–5 GB/year compressed | ~16M/year | Annual program year files |
| **OIG Exclusions** | ~78,927 total | ~3K–4K/year | ~30 MB (monthly CSV) | ~79K initial; ~4K/year | Monthly full-replacement CSV |
| **FDA Debarment** | ~100–200 total | Handful/year | <1 MB | ~200 (stable) | XLSX; rarely changes |
| **SAM.gov Exclusions** | Unknown (daily refresh) | Hundreds–thousands/year | ~10–20 MB (daily CSV) | ~10K–50K | Separate from OIG LEIE |
| **EPA ECHO** | 800K+ facilities | ~10K–20K enforcement actions/year | ~1–2 GB/year | ~20K–50K named individuals | Individual defendants in enforcement actions |

### Aggregate Estimates (In-Scope Sources, 2015 Cutoff)

#### Raw File Storage

| Source | Annual Ongoing | 2015–2024 Backfill |
|---|---|---|
| IRS Form 990 | ~5 GB/year compressed | ~40–50 GB |
| SEC EDGAR (Form 4/3/SC13G) | ~10–15 GB/year | ~80–120 GB |
| USASpending | ~5–10 GB/year | ~40–80 GB |
| FEC | ~4–5 GB/cycle (2-year) | ~25–35 GB |
| CMS Open Payments | ~3–5 GB/year | ~20–35 GB |
| DOJ Press Releases | ~0.5–1 GB/year | ~5–8 GB |
| EPA ECHO | ~1–2 GB/year | ~8–15 GB |
| All small sources combined | <0.5 GB/year | ~2 GB |
| **Total** | **~30–50 GB/year** | **~225–355 GB** |

| Horizon | Estimated Total Storage | R2 Cost/mo |
|---|---|---|
| Historical backfill complete (2015–present) | ~225–355 GB | ~$3.50–5.50 |
| Year 2 (backfill + ongoing) | ~275–410 GB | ~$4.00–6.00 |
| Year 5 (ongoing accumulation) | ~375–560 GB | ~$5.50–8.50 |

#### Database Row Counts

The `individual_documents` table uses a composite PK on `(individual_id, document_id, relationship)`. This means one row per person per bulk file, not one row per transaction — per-transaction detail is stored as JSON in `excerpt`/`identifiers`.

| Table | Estimated Rows (2015–present, in-scope only) |
|---|---|
| `individuals` (unique persons) | ~15–40M |
| `individual_documents` (linkages) | ~60–120M |
| `documents` (file metadata) | ~500K–2M |
| `scrape_jobs` / `scrape_errors` | Negligible |

---

## 2. Infrastructure Stack

### Compute

**Primary: Local hardware.** The system runs locally, eliminating the largest recurring cost (~$48/mo for cloud equivalent) at the tradeoff of availability risk.

- **RAM: 8 GB minimum, 16 GB recommended** — PostgreSQL `shared_buffers` (2 GB), peak scraper working memory for large bulk files (2–4 GB with Polars streaming), OS + headroom
- **Storage: 500 GB+ available** — PostgreSQL data directory will grow to ~100–300 GB; temp workspace for in-progress downloads (largest single file: EDGAR quarterly index ~3 GB compressed → ~10 GB uncompressed)
- **Always-on during scrape windows** — scrapers run on cron; machine must be up during scheduled windows

**VPS fallback (if local hardware unavailable):**
- Vultr Cloud Compute 4 GB (2 vCPU, 80 GB SSD): ~$24/mo
- Vultr 8 GB (4 vCPU, 160 GB SSD): ~$48/mo

### Storage

| System | Purpose | Cost |
|---|---|---|
| Local disk | PostgreSQL primary data store, temp download workspace | $0 |
| Cloudflare R2 | Raw source file archive; periodic `pg_dump` backups | $0.015/GB/mo |

R2 serves two roles: (1) raw file archive — every downloaded source file uploaded before processing; (2) database backup — scheduled `pg_dump` uploads, 30-day retention.

### API / Query Layer

**Undecided.** Options:
- **Local FastAPI process** — zero external cost; single-user only
- **Cloudflare Workers + Tunnel** — exposes local API via Tunnel with HTTPS and access controls; free tier sufficient
- **Minimal VPS as API host only** — $3.50/mo Vultr 1 GB VPS; connects to local PostgreSQL via Cloudflare Tunnel

### Monitoring

Structured logs via `structlog` to local log files with rotation. Email alerts via **Resend** (free tier: 3,000 emails/mo) on scrape failures. Daily summary email as a heartbeat.

### Estimated Monthly Cost

| Component | Cost |
|---|---|
| Local hardware | $0 (existing) |
| R2 raw file archive (~300 GB at backfill complete) | ~$4.50 |
| R2 DB backups (~30 days × ~30 GB compressed dump) | ~$1.50 |
| R2 ongoing (annual growth ~35 GB) | +~$0.50/mo/year |
| Email alerts (Resend free tier) | $0 |
| **Total** | **~$6–8/mo** |

---

## 3. Data Architecture

### Database Schema (PostgreSQL)

```sql
-- Named individuals extracted from any source
CREATE TABLE individuals (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,      -- normalized: lowercase, stripped punctuation
    aliases     JSONB,              -- array of alternate name strings
    merge_group_id BIGINT,          -- nullable; reserved for future entity resolution
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX individuals_fts_idx  ON individuals USING GIN (to_tsvector('english', name));
CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);

-- Every raw file stored in R2
CREATE TABLE documents (
    id          BIGSERIAL PRIMARY KEY,
    r2_key      TEXT NOT NULL UNIQUE,
    source_id   TEXT NOT NULL,      -- e.g. "fec", "ofac_sdn"
    source_url  TEXT,
    captured_at TIMESTAMPTZ NOT NULL,
    file_hash   TEXT NOT NULL,      -- SHA-256, used for dedup
    file_size   BIGINT,
    doc_type    TEXT,               -- "csv", "xml", "xlsx", "html"
    period      TEXT                -- e.g. "2024-Q1", "2025-04-17"
);
CREATE INDEX documents_source_idx ON documents (source_id, captured_at DESC);

-- Individual ↔ document linkage with role context
CREATE TABLE individual_documents (
    individual_id   BIGINT NOT NULL REFERENCES individuals(id),
    document_id     BIGINT NOT NULL REFERENCES documents(id),
    relationship    TEXT NOT NULL,  -- "fec_donor", "ofac_sanctioned", "lda_lobbyist", etc.
    excerpt         TEXT,           -- relevant snippet from document
    identifiers     JSONB,          -- {"fec_id": "...", "npi": "...", "ein": "..."}
    PRIMARY KEY (individual_id, document_id, relationship)
);
CREATE INDEX ind_docs_individual_idx ON individual_documents (individual_id);
CREATE INDEX ind_docs_document_idx   ON individual_documents (document_id);

-- Scrape job tracking
CREATE TABLE scrape_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL,  -- "running", "success", "partial", "failed"
    records_processed   INTEGER DEFAULT 0,
    records_new         INTEGER DEFAULT 0,
    errors_count        INTEGER DEFAULT 0,
    notes               TEXT
);
CREATE INDEX scrape_jobs_source_idx ON scrape_jobs (source_id, started_at DESC);

-- Per-source incremental state (dedup tracking)
CREATE TABLE scrape_state (
    source_id       TEXT PRIMARY KEY,
    last_run_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    cursor          TEXT,   -- source-specific: etag, max_id, last_date, file_hash, etc.
    metadata        JSONB   -- any extra state needed by the scraper
);

-- Structured error log (never truncated — auditability mechanism)
CREATE TABLE scrape_errors (
    id          BIGSERIAL PRIMARY KEY,
    job_id      BIGINT REFERENCES scrape_jobs(id),
    level       TEXT,       -- "warning", "error", "critical"
    message     TEXT,
    context     JSONB,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### R2 Key Convention

```
raw/{source_id}/{period}/{filename}

Examples:
  raw/fec/2024-Q1/indiv24.zip
  raw/ofac_sdn/2025-04-17/sdn.xml
  raw/doj_press/2025-04-17/pr-2025-04-17-143022.html
  raw/sec_edgar/2025-Q1/form.idx
```

### Name Normalization

All names stored in `name_norm` must be processed identically at ingest and at query time — centralized in a single shared function:

1. Unicode NFKD normalization, strip combining characters (accents)
2. Lowercase
3. Strip punctuation except hyphens and apostrophes within words
4. Collapse whitespace

---

## 4. Scraper Architecture

### Module Interface

```python
class BaseScraper:
    source_id: str          # snake_case, matches scrape_state.source_id
    schedule: str           # cron expression
    doc_type: str           # default doc_type for this source's documents

    def discover(self, state: dict) -> list[DownloadTarget]:
        """Return list of files/pages to fetch. Use state to skip already-fetched."""

    def fetch(self, target: DownloadTarget) -> bytes:
        """Download raw content. Raise on HTTP error."""

    def parse(self, raw: bytes, target: DownloadTarget) -> list[Record]:
        """Extract structured records. Each record identifies at least one individual."""

    def store(self, records: list[Record], raw: bytes, target: DownloadTarget):
        """Write raw file to R2. Write records + individual linkages to PostgreSQL."""
```

### Job Runner

APScheduler on the host machine. On each run:

1. Load `scrape_state` for the source from PostgreSQL
2. Call `discover()` — returns only new/changed targets based on state
3. Insert a `scrape_jobs` row with status `running`
4. For each target: fetch → parse → store, catching and logging errors per-record
5. Update `scrape_state` cursor on success
6. Update `scrape_jobs` row to `success`, `partial`, or `failed`
7. POST completion event to monitoring endpoint

Deployment model (single process vs. systemd timers) to be decided after scale validation.

### Error Handling Policy

- **HTTP 4xx (not 429)**: Log as error, mark target as failed, continue with remaining targets
- **HTTP 429 / rate limit**: Exponential backoff up to 3 retries, then log and skip
- **HTTP 5xx**: Retry up to 3 times with backoff, then log as error
- **Parse errors on a record**: Log as warning with raw excerpt, skip record, continue
- **Parse errors on entire file**: Log as critical, mark job `partial`, do not update cursor
- **R2 upload failure**: Retry 3 times, then mark job `failed` and halt — never write PostgreSQL records without the corresponding R2 file

A job completes as `partial` if >0 records succeeded and >0 failed. The cursor advances only to the last fully-successful point.

### Bulk Load Strategy

For all large bulk sources (FEC, EDGAR, IRS 990, CMS, USASpending):
- Write standalone `bootstrap_{source}.py` scripts (not part of the recurring scheduler)
- Process one year at a time: stream download → upload to R2 → stream parse → bulk `COPY` to PostgreSQL → delete temp file
- Never hold an entire uncompressed file in memory; use Polars `scan_csv()` (lazy) throughout
- Drop indexes before bulk COPY, recreate after
- **Historical cutoff: 2015.** No data before 2015 is loaded.

---

## 5. In-Scope Data Sources

### Sources Overview

| # | Source ID | Access Method | Auth Required | Rate Limit | Update Freq |
|---|---|---|---|---|---|
| 1 | `fec` | S3 bulk ZIP download | None | None (S3) | Daily / cycle |
| 2 | `sec_edgar` | REST API + nightly bulk ZIP | None (User-Agent req'd) | 10 req/sec | Nightly ~3am ET |
| 3 | `usaspending` | Bulk download API | None | Undocumented | Ongoing |
| 4 | `irs_990` | Monthly ZIP (XML) via IRS TEOS | None | ~2 req/sec | Monthly |
| 5 | `doj_press` | RSS feeds + HTML | None | ~1 req/sec | Continuous |
| 6 | `ofac_sdn` | Direct XML download | None | None | Daily |
| 7 | `atf_ffl` | Monthly text file download | None (verify) | None | Monthly |
| 8 | `lda` | REST API v1 | Free API key req'd | 120 req/min | Quarterly |
| 9 | `fara` | Bulk CSV/XML ZIP | None | None | Daily |
| 10 | `cms_open_payments` | Annual bulk CSV ZIP | None | None | Annually (June) |
| 11 | `oig_exclusions` | Single CSV (fixed URL) | None | None | Monthly |
| 12 | `fda_debarment` | XLSX download | None | None | Ad-hoc |
| 13 | `sam_exclusions` | REST API v4 | Free API key req'd | 1,000 req/day | Daily |
| 14 | `epa_echo` | REST API + bulk CSV | None | ~2 req/sec | Varies |

---

### 1. FEC Individual Contributions (`fec`)

**Permissibility:** Explicitly permitted. Bulk S3 downloads are FEC's intended mechanism for automated bulk access. robots.txt blocks interactive query endpoints only.

**Access Method:** Bulk ZIP files on AWS GovCloud S3 — direct file downloads, no scraping.

```
# Bulk individual contributions by cycle year
https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com/bulk-downloads/[YEAR]/indiv[YY].zip

# OpenFEC API (supplementary — incremental updates after bulk load)
https://api.open.fec.gov/v1/schedules/schedule_a/
```

**Authentication:** None for S3 bulk downloads. Optional free API key for OpenFEC raises rate limit from 20 to 1,000 req/hour.

**Rate Limits:** S3 — none. OpenFEC API — 20 req/hour unauthenticated; 1,000 req/hour with free key.

**Incremental Strategy:**
- Initial load: Download `indiv15.zip` through `indiv24.zip` (one ZIP per 2-year cycle)
- Ongoing: OpenFEC API `/schedules/schedule_a/` with `last_index` cursor for current-cycle additions; download full ZIP at cycle close
- Dedup: Store SHA-256 of each ZIP; store last-seen transaction ID from API

**File Format:**
- ZIP containing a single pipe-delimited (`|`) CSV
- No header row — column order from FEC data dictionary
- Size: ~3–5 GB compressed per 2-year cycle; ~15–20 GB uncompressed
- Key fields: `NAME`, `ENTITY_TP`, `CITY`, `STATE`, `ZIP_CODE`, `EMPLOYER`, `OCCUPATION`, `TRANSACTION_DT`, `TRANSACTION_AMT`, `CMTE_ID`, `SUB_ID`

**Parse Targets:**
- Relationship: `fec_donor`
- Filter: `ENTITY_TP = 'IND'` (excludes PACs and businesses)
- Name from `NAME` field (format: `LAST, FIRST MIDDLE`)
- Source identifier: `SUB_ID`
- Additional context (stored in `identifiers` JSON): employer, occupation, amount, committee, date

**Gotchas:**
- Pipe-delimited; specify `sep='|'` in Polars
- No header row; column order must be sourced from FEC data dictionary
- Cycle files use 2-year naming: `indiv20.zip` covers 2019–2020
- Always use Polars `scan_csv()` (lazy); never `read_csv()`

---

### 2. SEC EDGAR (`sec_edgar`)

**Permissibility:** Explicitly permitted and designed for programmatic access. SEC publishes developer documentation at `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`. A `User-Agent` header with organization name and email is **required**; failure results in blocks.

**Access Method:** Two complementary interfaces:
1. Quarterly full-index files — list all filings per quarter; filter to in-scope form types
2. Daily index — incremental, for ongoing updates after initial load

```
# Quarterly full-index
https://www.sec.gov/Archives/edgar/full-index/[YEAR]/QTR[1-4]/form.idx

# Daily index (incremental)
https://www.sec.gov/Archives/edgar/daily-index/[YEAR]/QTR[1-4]/form[MMDDYYYY].idx

# Individual filing document
https://www.sec.gov/Archives/edgar/data/{CIK}/{accession-no}/{filename}

# Company submissions via API (JSON)
https://data.sec.gov/submissions/CIK{10-digit-cik}.json
```

**Authentication:** None. Required `User-Agent` header: `User-Agent: PubRecSearch {email}`

**Rate Limits:** 10 req/sec hard limit. Implement token bucket or `asyncio.Semaphore` enforcing ≤10 req/sec.

**Incremental Strategy:**
- Initial load (2015–present): Download quarterly `form.idx` for 2015Q1 through current quarter; filter for form types `4`, `3`, `SC 13G`, `SC 13G/A`, `SC 13D`, `SC 13D/A`; download each matching filing
- Ongoing: Poll daily index for current quarter; process new entries matching in-scope form types
- Dedup: Store processed accession numbers (or max accession date) in `scrape_state.cursor`

**File Format:**
- Quarterly index: fixed-width text (`.idx`) — columns: Company Name, Form Type, CIK, Date Filed, Filename
- Filing documents: XML (Form 4/3), HTML/SGML (older filings)
- Size: Quarterly `form.idx` ~5–10 MB; individual Form 4 XML ~5–15 KB each

**Parse Targets:**
- Relationship: `sec_insider` (Form 4/3), `sec_large_shareholder` (SC 13G/D)
- From Form 4 XML: `<reportingOwner>` → name, CIK; `<issuer>` → company; transaction details
- From SC 13G/D: filer name from cover page (XML header)
- Source identifier: EDGAR accession number (`{CIK}-{YY}-{NNNNNN}`)

**Gotchas:**
- Form 4 XML schema changed over time; handle both pre-2004 SGML and modern XML variants
- Some filings are HTML or text, not XML; detect by file extension in the index
- CIK numbers are zero-padded to 10 digits in API paths but not always in index files
- Rate limit applies per IP across all concurrent requests combined

---

### 3. USASpending.gov (`usaspending`)

**Permissibility:** Permitted via official public API. robots.txt blocks URLs with query parameters but not the REST API at `api.usaspending.gov`.

**Access Method:** Pre-generated annual CSV archive files; best for initial load.

```
# Award Data Archive page (verify current filenames here)
https://www.usaspending.gov/download_center/award_data_archive

# File name patterns (date-stamped; verify before coding fixed URLs)
# Contracts: FY[YEAR]_All_Contracts_Full_20YYMMDD.zip
# Assistance: FY[YEAR]_All_Assistance_Full_20YYMMDD.zip
```

**Authentication:** None. No API key.

**Rate Limits:** Not documented. Treat conservatively: max 5 concurrent requests; 1-second delay between requests.

**Incremental Strategy:**
- Initial load: Download annual award archive ZIPs for FY2015–FY2024; filter individual recipients at parse time
- Ongoing: Poll archive page monthly; quarterly updated files published for current fiscal year; compare file modification date or hash against cursor
- Dedup: Store filename + SHA-256 of each downloaded archive in `scrape_state`

**File Format:**
- ZIP containing multiple CSV files (split by size threshold)
- Size: FY annual contracts ~2–8 GB compressed; assistance ~500 MB–2 GB
- Key fields: `recipient_name`, `recipient_uei`, `business_types_codes`, `recipient_address_line_1`, `recipient_city_name`, `recipient_state_code`, `award_id_fain`, `award_id_piid`

**Parse Targets:**
- Relationship: `usaspending_recipient`
- Filter: `business_types_codes` contains `'I'` (Individual) or sole proprietor indicators (`'2X'`, `'23'`)
- Individual name from `recipient_name`
- Source identifier: `award_id_fain` or `award_id_piid`

**Gotchas:**
- `business_types_codes` is multi-value (comma-separated); filter with `str.contains('I')`
- Individual-recipient records are ~1–5% of total awards; most are companies
- Archive page URL patterns include date stamps that change — verify before coding fixed URLs

---

### 4. IRS Form 990 (`irs_990`)

**Permissibility:** Explicitly permitted. IRS publishes 990 EFILE data for public download. **Note: The old AWS S3 bucket (`s3://irs-form-990/`) was deprecated December 31, 2021 and is no longer updated.** Use IRS TEOS instead.

**Access Method:** Monthly ZIP files containing XML 990 filings, published at the IRS TEOS download page.

```
# TEOS download page (verify current file listing here)
https://www.irs.gov/charities-non-profits/form-990-series-downloads

# Monthly XML ZIPs
https://apps.irs.gov/pub/epostcard/990/xml/[YEAR]/[YEAR]_TEOS_XML_[MM]A.zip

# Annual CSV index
https://apps.irs.gov/pub/epostcard/990/xml/[YEAR]/index_[YEAR].csv
```

**Authentication:** None.

**Rate Limits:** Not documented; treat as standard web server (max 2 req/sec, single-threaded).

**Incremental Strategy:**
- Initial load: Download `index_{YEAR}.csv` for 2015–current; download monthly XML ZIPs for each year 2015–present
- Ongoing: New monthly ZIPs published throughout the year; poll download page monthly; compare against stored list of downloaded files
- Dedup: Store `{YEAR}_TEOS_XML_{MM}A.zip` filenames in `scrape_state`; individual filing dedup by `ReturnId` or `EIN + TaxPeriodEndDt`

**File Format:**
- ZIP containing individual XML files, one per 990 filing
- Size: Each monthly ZIP ~50–300 MB; each individual XML ~20–500 KB
- Key fields: `ReturnHeader/Filer/EIN`, `ReturnHeader/TaxPeriodEndDt`, `IRS990/Form990PartVIISectionA`

**Parse Targets:**
- Relationship: `nonprofit_officer`, `nonprofit_director`, `nonprofit_contractor`, `nonprofit_hce`
- Source: `IRS990/Form990PartVIISectionA` — officer/director compensation table; also `IRS990ScheduleL` (loans to/from officers)
- Name from `PersonNm` or `BusinessName/BusinessNameLine1Txt`; role from `TitleTxt`
- Source identifier: `ReturnId` or `EIN + TaxPeriodEndDt`

**Gotchas:**
- 990 XML schema has many versions; namespace/element paths differ between 990, 990-EZ, and 990-PF
- `YEAR` in filename is the processing year, not the tax year; a 2024 file may contain 2023 tax year returns
- 990-N (e-postcard) filers are not in the XML ZIPs and contain no individual data anyway
- Do not use the old S3 bucket; it stopped updating December 31, 2021

---

### 5. DOJ Press Releases (`doj_press`)

**Permissibility:** Permitted via RSS and documented API. DOJ explicitly provides automated access endpoints at `https://www.justice.gov/developer`.

**Access Method:** RSS feeds (primary); HTML scraping for initial historical load.

```
# All DOJ press releases RSS
https://www.justice.gov/news/press-releases/rss.xml

# Component-specific RSS feeds
https://www.justice.gov/usao/rss     # US Attorneys Office
https://www.justice.gov/opa/rss      # Office of Public Affairs

# DOJ Developer API (JSON)
https://www.justice.gov/api/resources/press-releases.json

# HTML listing (initial load)
https://www.justice.gov/news/press-releases?page={N}
```

**Authentication:** None.

**Rate Limits:** Max 1 req/sec for HTML pages; burst-free for RSS.

**Incremental Strategy:**
- Initial load (2015–present): Paginate HTML listing backwards from current date to 2015-01-01; fetch each release page for full text
- Ongoing: Poll main RSS feed daily; parse new items since last `pubDate` stored in cursor
- Dedup: Store last-seen RSS `guid` (DOJ URL) as cursor

**File Format:**
- RSS items: title, link, pubDate, description (partial text)
- Full press release: HTML; full text in `<div class="field-items">`

**Parse Targets:**
- Relationship: `doj_defendant`, `doj_subject`
- Deterministic extraction (no LLM):
  1. Title regex: `"United States v. [Name]"`, `"[Name] Sentenced"`, `"[Name] Pleads Guilty"`, `"[Name] Charged"`, `"[Name] Convicted"`, `"[Name] Indicted"`
  2. Body regex: `defendant [First Last]`, `[First Last], of [City]`, `[First Last], age [NN]`
  3. Flag confidence: `high` for "v." pattern, `medium` for body regex
- Store full title + first 500 chars of body as `excerpt`
- Source identifier: DOJ press release URL (slug is stable)

**Gotchas:**
- Named entity extraction from free text will produce false positives and false negatives; store confidence level and raw excerpt — this is expected and acceptable
- Some press releases name organizations rather than individuals; filter to releases matching human name patterns
- Do not follow PACER case links sometimes embedded in DOJ press releases

---

### 6. OFAC SDN List (`ofac_sdn`)

**Permissibility:** Explicitly permitted and encouraged. OFAC's Sanctions List Service documentation explicitly states firms should set up scheduled automated downloads.

**Access Method:** Direct file download. Full SDN list updated daily at fixed URLs.

```
# Primary SDN XML
https://www.treasury.gov/ofac/downloads/sdn.xml

# SDN delimited format (supplementary)
https://www.treasury.gov/ofac/downloads/sdn.csv
https://www.treasury.gov/ofac/downloads/alt.csv   # aliases
https://www.treasury.gov/ofac/downloads/add.csv   # addresses

# Advanced Sanctions Data Standard (richer schema)
https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML
```

**Authentication:** None. HTTPS required.

**Rate Limits:** None documented. One download per scheduled run is the intended pattern.

**Incremental Strategy:**
- Full replacement: SDN list is not append-only; entries are added, modified, and removed with no change log
- Dedup: Store SHA-256 hash of downloaded XML in `scrape_state.cursor`; re-parse only if hash changed
- Schedule: Daily at off-peak hours

**File Format:**
- Primary: XML (`sdn.xml`) — most complete; ~25–30 MB
- `consolidated.xml` covers additional sanction programs; ~60 MB

**Parse Targets:**
- Relationship: `ofac_sanctioned`
- Filter: `<sdnType>` = `"Individual"` only (exclude Entity, Vessel, Aircraft)
- Name from `<lastName>`, `<firstName>`; aliases from `<akaList>` → store in `individuals.aliases`
- Additional: nationality, DOB, place of birth, ID documents → `identifiers` JSON
- Source identifier: OFAC `<uid>` (integer, stable per entry)

**Gotchas:**
- `sdn.xml` and `consolidated.xml` use slightly different schemas; pick one and be consistent
- Aliases in `<akaList>` are separate name entries; create alias records in `individuals.aliases`
- The ADVANCED XML format has richer data but more complex schema — verify against OFAC docs before using

---

### 7. ATF Federal Firearms Licensee List (`atf_ffl`)

**Permissibility:** Likely permitted; verify the specific download mechanism before implementation.

**Access Method:** Monthly pipe-delimited text files from ATF listing page. Requires verification — check `https://www.atf.gov/robots.txt` and confirm files are directly downloadable without login.

```
# ATF listing page (check for monthly download links)
https://www.atf.gov/firearms/listing-federal-firearms-licensees

# eZ Check download (check if login required)
https://fflezcheck.atf.gov/FFLEzCheck/fflDownloadDisplay.action
```

**Authentication:** Unknown; may be none for the ATF listing page links.

**Rate Limits:** Not documented. Monthly download; single file retrieval.

**Incremental Strategy:**
- Monthly replacement: Download new monthly file when published; store filename or file hash as cursor
- ATF FFL is a current-state snapshot; not append-only

**File Format:**
- Pipe-delimited text file (`.txt`)
- Encoding: Try UTF-8 first; fall back to `latin-1` on decode error
- Size: ~10–15 MB per monthly file
- No header row — column order from ATF documentation
- Key fields: `license_regn`, `license_dist`, `license_cnty`, `license_type`, `expir_date`, `lic_seqn`, `lic_name`, `lic_bus_name`, `premise_street`, `premise_city`, `premise_state`, `premise_zip_code`, `voice_phone`

**Parse Targets:**
- Relationship: `atf_ffl_licensee`
- Individual identification: sole proprietors where `lic_name` differs from `lic_bus_name`, or where `lic_bus_name` matches individual name patterns
- License type filter: Type 01 (Dealer), Type 03 (Collector), Type 06 (Ammo Manufacturer), Type 07 (FFL Manufacturer)
- Source identifier: Composite of `license_regn + license_dist + license_type + license_cnty + lic_seqn`

**Gotchas:**
- `lic_name` is the licensee (individual name or officer); `lic_bus_name` is business name — sole proprietors often have personal names in both fields
- No header row; column order must come from ATF documentation
- Collectors (Type 03) and some manufacturers may be excluded from public download per ATF policy

---

### 8. LDA Lobbying Disclosure (`lda`)

**Permissibility:** Explicitly permitted with documented ToS at `https://lda.senate.gov/api/tos/`. API key registration required before first use.

**Access Method:** REST API v1 (JSON). Free API key required.

**Registration:** `https://lda.senate.gov/api/register/`

```
# API base
https://lda.senate.gov/api/v1/

# Filings (LD-2 quarterly activity reports)
https://lda.senate.gov/api/v1/filings/?filing_year={YYYY}&filing_period={period}&page={N}

# Registrations (LD-1)
https://lda.senate.gov/api/v1/registrations/?page={N}

# Contributions (LD-203)
https://lda.senate.gov/api/v1/contributions/?filing_year={YYYY}&page={N}

# API documentation
https://lda.senate.gov/api/redoc/v1/
```

**Authentication:** Required. Pass as `Authorization: Token {api_key}` header.

**Rate Limits:** 120 req/min with API key; 15 req/min without (do not use unauthenticated).

**Incremental Strategy:**
- Initial load: Paginate all filings from `filing_year=2015` forward by year and period (Q1–Q4)
- Ongoing: Query current year/quarter; filter by `received_dt` >= last run timestamp stored in cursor
- Pagination: `next` URL provided in response

**File Format:**
- JSON responses
- Key fields: `registrant.name`, `registrant.id`, `lobbyists[].lobbyist_first_name`, `lobbyists[].lobbyist_last_name`, `filing_uuid`, `filing_type`, `received_dt`

**Parse Targets:**
- Relationship: `lda_lobbyist`
- Individual from `lobbyists[].lobbyist_first_name + lobbyist_last_name`
- Also: `registrant.contact_name` (may be a separate individual)
- Source identifier: `filing_uuid`

**Gotchas:**
- `lobbyists` array is empty for LD-1 registrations; LD-2 filings have the actual lobbyist lists
- Amendments (`filing_type = "AMENDMENT"`) update earlier filings; use amendment as canonical record
- `income` and `expenses` fields contain dollar amounts as strings (e.g., `"$1,000,000"`); strip commas and `$` before storing
- Query from 2015 using `filing_year__gte=2015` filter

---

### 9. FARA (`fara`)

**Permissibility:** Explicitly permitted. FARA provides a dedicated bulk data portal. No authentication required.

**Access Method:** Bulk ZIP downloads containing CSV files; updated daily.

```
# FARA Bulk Data portal
https://efile.fara.gov/ords/fara/f?p=API:BULKDATA

# Direct download URLs
https://efile.fara.gov/bulk/zip/FARA_All_Registrants.csv.zip
https://efile.fara.gov/bulk/zip/FARA_All_Short_Form.csv.zip  # Individual lobbyists (NSD-6)
https://efile.fara.gov/bulk/zip/FARA_All_Supplemental_Statements.csv.zip
```

**Authentication:** None.

**Rate Limits:** None documented. Daily download of full files is the intended pattern.

**Incremental Strategy:**
- Full replacement: Bulk files are full snapshots, not append-only
- Dedup: SHA-256 hash of downloaded ZIP stored as cursor; re-parse only on hash change
- Schedule: Daily

**File Format:**
- ZIP containing CSV files
- **Encoding: ISO-8859-1 (Latin-1)** — explicitly noted in FARA documentation; always specify `encoding='iso-8859-1'`
- Size: Full registrants CSV ZIP ~5–20 MB
- Key fields in `FARA_All_Registrants.csv`: `RegistrantID`, `Name`, `Address`, `RegistrationDate`, `TerminationDate`, `RegistrantType`

**Parse Targets:**
- Relationship: `fara_registrant`
- Filter `RegistrantType = "Individual"` for direct individual registrants
- For organizations: extract individuals from Short Form data (`FARA_All_Short_Form.csv`)
- Source identifier: `RegistrantID`

**Gotchas:**
- ISO-8859-1 encoding; parse will silently corrupt names if opened as UTF-8 — always specify encoding explicitly
- `TerminationDate` may be empty (still active) or populated (terminated); retain both
- Prioritize `FARA_All_Registrants` and `FARA_All_Short_Form` for individual extraction

---

### 10. CMS Open Payments (`cms_open_payments`)

**Permissibility:** Explicitly permitted. Published as part of the Physician Payments Sunshine Act transparency mandate.

**Access Method:** Annual bulk CSV downloads per program year (PY); published on or before June 30 each year.

```
# Open Payments download page (retrieve current URLs from here)
https://openpaymentsdata.cms.gov/
```

**Authentication:** None.

**Rate Limits:** Not documented. Annual bulk download pattern.

**Incremental Strategy:**
- Initial load: Download all available program year ZIPs PY2015–current
- Ongoing: New program year published annually by June 30; poll download page each July
- CMS refreshes prior-year data in January with late submissions; check already-downloaded years for updates via stored file hash
- Dedup: Track downloaded program years in `scrape_state`

**File Format:**
- ZIP containing multiple CSV files (General Payments, Research Payments, Ownership & Investment Interest)
- **Delimiter: Pipe (`|`) — NOT comma**
- Encoding: UTF-8
- Size: PY2024 ~3–5 GB compressed; exceeds Excel row limits — always use Polars streaming
- Key fields: `Covered_Recipient_First_Name`, `Covered_Recipient_Last_Name`, `Covered_Recipient_Type`, `Covered_Recipient_NPI`, `Total_Amount_of_Payment_USDollars`, `Date_of_Payment`, `Nature_of_Payment_or_Transfer_of_Value`, `Record_ID`

**Parse Targets:**
- Relationship: `cms_payment_recipient`, `cms_ownership_holder`
- Filter: `Covered_Recipient_Type` = `"Covered Recipient Physician"` or `"Covered Recipient Non-Physician Practitioner"`
- Individual name: `Covered_Recipient_First_Name + Covered_Recipient_Last_Name`
- Source identifier: `Record_ID` or `Covered_Recipient_NPI`

**Gotchas:**
- Pipe-delimited; specify `sep='|'` in Polars
- Each program year ZIP contains multiple CSV files; process all three and link to the same individual
- `Covered_Recipient_NPI` is a strong cross-source identifier; absent in some pre-2017 records
- CMS does not guarantee stable bulk file URLs year-over-year; must scrape download page to find current URLs

---

### 11. OIG Exclusions LEIE (`oig_exclusions`)

**Permissibility:** Explicitly permitted. OIG provides CSV downloads with right-click instructions implying direct/automated download is intended.

**Access Method:** Single CSV at a fixed URL, updated monthly (replaced in place).

```
# Current exclusions list (full replacement monthly)
https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv

# OIG LEIE API (supplementary)
https://api.exclusions.oig.hhs.gov/
```

**Authentication:** None.

**Rate Limits:** Not documented. Single file download.

**Incremental Strategy:**
- Full replacement: Download `UPDATED.csv` monthly; compare SHA-256 against stored hash; re-parse only on change
- Dedup: Hash of last-downloaded file; individual dedup by `EXCL_DATE + LASTNAME + FIRSTNAME + MIDNAME`

**File Format:**
- CSV (comma-delimited)
- Encoding: UTF-8
- Size: ~5–10 MB (~79,000 records)
- Key fields: `LASTNAME`, `FIRSTNAME`, `MIDNAME`, `BUSNAME`, `GENERAL`, `SPECIALTY`, `UPIN`, `NPI`, `DOB`, `ADDRESS`, `CITY`, `STATE`, `ZIP`, `EXCL_TYPE`, `EXCL_DATE`, `REINSTATE_DATE`, `WAIVERSTATE`

**Parse Targets:**
- Relationship: `oig_excluded`
- Filter: `BUSNAME` is empty (individual) or `LASTNAME` contains no LLC/Inc/Corp
- Name: `FIRSTNAME + MIDNAME + LASTNAME`
- Source identifier: `NPI` if present; else `UPIN`; fallback to composite name + `EXCL_DATE`

**Gotchas:**
- `BUSNAME` populated for organization exclusions; for individuals typically empty
- `REINSTATE_DATE` populated if exclusion was lifted; retain these records (historically significant)
- `NPI` absent for older records predating NPI system

---

### 12. FDA Debarment List (`fda_debarment`)

**Permissibility:** Permitted. FDA publishes the list publicly; no robots.txt restriction.

**Access Method:** XLSX files per debarment category, linked from the FDA debarment page. OpenFDA JSON API available as alternative.

```
# FDA debarment page (parse HTML to find current XLSX download URLs)
https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/fda-debarment-list-drug-product-applications

# OpenFDA APIs (alternative)
https://open.fda.gov/apis/
```

**Authentication:** None. OpenFDA: no key for <240 req/min; free key for higher limits.

**Rate Limits:** Not documented for XLSX download. Single-file retrieval.

**Incremental Strategy:**
- Full replacement: Download and re-parse full file on each run (very small dataset)
- Dedup: SHA-256 hash of downloaded file; re-parse only on change
- Schedule: Monthly

**File Format:**
- XLSX (multiple sheets, one per debarment category)
- Size: Very small (<1 MB)
- Key fields: Name, Debarment Type, Effective Date, Debarment Period

**Parse Targets:**
- Relationship: `fda_debarred`
- All entries on the individual debarment list are persons (company debarments are on a separate list)
- Source identifier: Name + Effective Date (no stable numeric ID)

**Gotchas:**
- FDA maintains multiple separate debarment lists (drug product applications, food import, drug import, tobacco); download and parse all relevant sheets
- Requires `openpyxl` library
- Debarment page HTML must be parsed to find current XLSX download URLs; file names include version numbers that change

---

### 13. SAM.gov Exclusions (`sam_exclusions`)

**Permissibility:** Explicitly permitted via official API. Free API key required.

**Access Method:** REST API v4 (JSON). V1–V3 were retired September 2024.

**Registration:** `https://sam.gov/profile/details`

```
# Production API base
https://api.sam.gov/entity-information/v4/exclusions

# API documentation
https://open.gsa.gov/api/exclusions-api/

# Example query: individual exclusions updated since a date
https://api.sam.gov/entity-information/v4/exclusions?api_key={KEY}&exclusionType=Individual&updatedDate=[MM/DD/YYYY,MM/DD/YYYY]&page=0&size=100
```

**Authentication:** Required. Free personal API key from `https://sam.gov/profile/details`. Pass as query parameter `api_key={KEY}`.

**Rate Limits:** 1,000 requests/day (non-federal personal key). JSON: up to 10,000 records per request. CSV extract: up to 1,000,000 records per async request.

**Incremental Strategy:**
- Initial load: Use CSV extract endpoint (async delivery) with `exclusionType=Individual` — returns all individual exclusions in a single file
- Ongoing: Query with `updatedDate` filter for records updated since last run
- Dedup: Store last successful run date as cursor

**File Format:**
- API (JSON): Paginated; `entityData` array per page; `totalRecords` for pagination math
- CSV extract: Async; returns download URL via polling
- Key fields: `exclusionDetails.exclusionName.firstName`, `.lastName`, `.middleName`, `exclusionDetails.classification`, `exclusionDetails.exclusionProgram`, `exclusionDetails.activationDate`, `exclusionDetails.terminationDate`, `exclusionDetails.samNumber`

**Parse Targets:**
- Relationship: `sam_excluded`
- Filter: `exclusionDetails.classification = "Individual"`
- Source identifier: `exclusionDetails.samNumber` or composite name + `activationDate`

**Gotchas:**
- V1–V3 APIs are dead (retired Sept 2024); use only V4
- `updatedDate` filter format: `MM/DD/YYYY,MM/DD/YYYY` (comma-separated in square brackets as URL parameter)
- CSV async extract requires polling a status endpoint; implement polling rather than awaiting email
- SAM.gov exclusions overlap with but are not identical to OIG LEIE; capture both

---

### 14. EPA ECHO (`epa_echo`)

**Permissibility:** Explicitly permitted via documented public API. robots.txt blocks admin/user paths but not API endpoints.

**Access Method:** REST API (HTTP GET) and bulk CSV downloads. Use bulk download for initial load.

```
# ECHO Web Services documentation
https://echo.epa.gov/tools/web-services

# Enforcement Case Search (filter by individual defendants)
https://echodata.epa.gov/echo/case_rest_services.get_cases?output=JSON&p_defendant_type=I

# Case detail (defendants per case)
https://echodata.epa.gov/echo/case_rest_services.get_case_info?output=JSON&p_id={case_id}

# Bulk data downloads (CSV — preferred for initial load)
https://echo.epa.gov/tools/data-downloads
```

**Authentication:** None.

**Rate Limits:** Not explicitly documented. Treat conservatively: 2 req/sec, sequential.

**Incremental Strategy:**
- Initial load: Use bulk data downloads — pre-generated CSVs covering all enforcement cases; filter individual defendants at parse time
- Ongoing: Query enforcement cases API with `p_case_activity_date_begin` for cases active since last run; store last run date as cursor
- Dedup: Store ECHO case ID (`p_id`) for all processed cases

**File Format:**
- API: JSON
- Bulk downloads: CSV/ZIP
- Key fields: `case_number`, `case_name`, `defendant_name`, `defendant_type` (I = Individual, C = Company), `program_acronym`, `penalty_assessed_amt`, `case_activity_date`

**Parse Targets:**
- Relationship: `epa_enforcement_defendant`
- Filter: `defendant_type = "I"` (Individual)
- Individual name from `defendant_name`
- Source identifier: ECHO `case_number`

**Gotchas:**
- Most cases are corporate defendants; use `p_defendant_type=I` to reduce volume
- EPA enforcement spans multiple programs (CAA, CWA, RCRA, SDWA, TSCA, FIFRA); use bulk download for completeness
- `defendant_name` format varies: "LAST, FIRST", "FIRST LAST", "DBA [business]" — normalize during parse
- Bulk download column names may differ from API response field names; verify against actual files

---

## 6. Query API

### Endpoints

```
GET  /search?q={name}&source={source_id}&limit=20&offset=0
     → pg_trgm/tsvector query against individuals; returns individuals + document count per source

GET  /individual/{id}
     → Individual record + all linked documents with relationship and excerpt

GET  /individual/{id}/documents?source={source_id}
     → Documents for an individual, optionally filtered by source

GET  /document/{id}
     → Document metadata + presigned R2 URL (TTL: 1 hour)

GET  /sources
     → All sources with last_success_at, records_processed, status of most recent job

GET  /health
     → Liveness check
```

### Search Behavior

Search queries are normalized identically to ingest (same normalization function). PostgreSQL `pg_trgm` supports fuzzy name matching (`similarity(name_norm, $query) > 0.4`); `tsvector` full-text search handles tokenized name queries. Results ranked by similarity score with a secondary sort on most-recent document capture date.

Deployment choice (local FastAPI vs. Cloudflare Workers + Tunnel) is deferred until the local/VPS compute decision is finalized.

---

## 7. Monitoring

### Scrape Health Alerts

Alert conditions:
- Job status is `failed` or `partial` with `errors_count > threshold`
- Source has not had a `success` run in more than `N` days (configurable per source)
- `records_new == 0` for a source that normally produces new records (potential silent failure)

Alerts delivered via Resend email.

### Scrape Dashboard

The `/sources` endpoint returns enough data to build a simple status dashboard: last run time, success/fail, record counts, error counts. A static HTML page served from R2 can poll this endpoint.

### Audit Log

Every `scrape_jobs` row is a permanent audit record. The `scrape_errors` table retains per-record error context. Neither table is ever truncated.

---

## 8. Architectural Decisions (Resolved)

**Database: PostgreSQL (local).** D1 is not viable — FEC alone exceeds D1's 10 GB storage limit; `individual_documents` will reach 60–120M rows well beyond D1's capacity; no bulk insert/COPY support in D1.

**Entity resolution: None at ingest (Option A).** Each source record creates a new `individuals` row. All records stored as-is. Entity resolution is a separate downstream component and is out of scope for this project. A nullable `merge_group_id` column is reserved for future use without schema migration.

**Historical cutoff: 2015.** No data before 2015 is loaded. Sources with annual/quarterly file boundaries are bootstrapped year-by-year from 2015 forward.

**PDF handling: Not required.** All financial disclosures (House, Senate, OGE) are out of scope. No in-scope source requires PDF parsing. `pdfplumber` is not a dependency.

**NSOPW: Excluded.** ToS prohibits bulk enumeration. No scraper will be built.

**Deployment model: Decided at runtime.** Single APScheduler process is the starting point; migrate to systemd timers per-scraper if memory pressure or interference becomes a problem.

---

## 9. Out-of-Scope Sources

| Source | Reason |
|---|---|
| NSOPW | ToS prohibits bulk enumeration |
| PACER | Requires account + $0.10/page fee; no bulk access |
| BOP Inmate Locator | No bulk download; enumeration raises ToS concerns |
| FCC ULS | Ham radio licensing; low investigative value |
| FAA Airmen | Licensing database; lower investigative value |
| NPPES NPI Registry | Healthcare provider licensing; limited relevance relative to bulk (9.3 GB/month) |
| USPTO Patents | IP ownership data; deferred as a category |
| USPTO Trademarks | IP ownership data; deferred as a category |
| Copyright Office | IP ownership data; deferred as a category |
| House Financial Disclosures | PDF/XML complexity; revisit if PDF extraction is resolved |
| Senate Financial Disclosures | PDF-only; high extraction complexity for low volume |
| OGE Financial Disclosures | Mostly PDFs; similar to Senate disclosures |
| Congressional Record | Free-text extraction too complex; structured member data covered by FEC/LDA |
| NTSB Accident Database | Small dataset; named individuals are crew members; limited investigative scope |
| CPSC Recall Database | Named individuals extremely rare; primarily product/company data |

---

## 10. Implementation Phases

### Phase 1 — Foundation + Small Bulk Sources

- Local machine setup: PostgreSQL install, schema creation, R2 bucket, Cloudflare credentials
- Job runner, base scraper interface, R2 upload + PostgreSQL write utilities, Polars streaming helpers
- Name normalization function (shared between all scrapers and query API)
- Scrapers: OFAC SDN, ATF FFL, OIG Exclusions, SAM.gov Exclusions, FDA Debarment
- Query API (`/search`, `/individual`, `/sources`) — FastAPI locally; deployment approach TBD
- Email monitoring alerts (Resend) + daily heartbeat email

### Phase 2 — Large Bulk Sources + Historical Backfill (2015–present)

- FEC (current cycle first, then historical bootstrap 2015–2024)
- IRS Form 990 (IRS TEOS bulk XML, 2015–present)
- CMS Open Payments (2015–present)
- SEC EDGAR (Form 4/3/SC13G, 2015–present)
- USASpending.gov (FY2015–present, individual recipients only)
- Historical bootstrap scripts (standalone, run year-by-year)
- Scrape health dashboard

### Phase 3 — Structured API Sources

- LDA Lobbying Disclosure
- FARA

### Phase 4 — HTML Scraping Sources (higher maintenance)

- DOJ Press Releases (regex defendant extraction)
- EPA ECHO (enforcement actions API + bulk download)

### Phase 5 — Open Web: News Feeds, Court Records & Agency Newsrooms

Broad-coverage named-entity capture from press releases, court records, and news wires beyond the structured federal databases covered in Phases 1–4. Sources in this phase are higher-maintenance (HTML scraping, RSS parsing, free-text NLP) but dramatically expand coverage of criminal charges, civil enforcement, and general public record activity.

#### 5a. Federal Agency Enforcement Press Releases

Each of these agencies publishes RSS feeds and/or paginated HTML archives of enforcement press releases naming individual subjects. Approach mirrors `doj_press`: RSS for ongoing, paginated HTML for historical backfill (2015–present). Defendant extraction uses the same regex pipeline as DOJ.

| Source ID | Agency | RSS / Feed URL | Notes |
|---|---|---|---|
| `sec_enforcement` | SEC Litigation Releases | `https://www.sec.gov/litigation/litreleases.htm` + RSS | Named defendants in civil/criminal SEC actions; also check `https://www.sec.gov/divisions/enforce/enforc.htm` |
| `fbi_press` | FBI Press Releases | `https://www.fbi.gov/news/press-releases` (RSS available) | Arrests, convictions, charges; often duplicates DOJ but includes field office releases |
| `ftc_enforcement` | FTC Enforcement Actions | `https://www.ftc.gov/news-events/news/press-releases/rss.xml` | Individuals named in deceptive practice cases, identity theft, fraud actions |
| `cfpb_enforcement` | CFPB Enforcement Actions | `https://www.consumerfinance.gov/action-center/rss/` | Individuals in mortgage fraud, debt collection, credit card enforcement |
| `irs_ci_press` | IRS Criminal Investigation | `https://www.irs.gov/newsroom/irs-criminal-investigation-press-releases` | Tax fraud, money laundering, structuring convictions; very high individual name density |
| `fincen_enforcement` | FinCEN Enforcement | `https://www.fincen.gov/news-room/press-releases` | BSA violations, money laundering designations; named individuals and institutions |
| `cftc_enforcement` | CFTC Enforcement Actions | `https://www.cftc.gov/PressRoom/PressReleases` | Commodity fraud, futures manipulation; individual traders named |
| `fdic_enforcement` | FDIC Enforcement Orders | `https://www.fdic.gov/regulations/enforcement/orders/` | Prohibition orders, civil money penalties against bank insiders; structured CSV also available |
| `occ_enforcement` | OCC Enforcement Actions | `https://www.occ.gov/news-issuances/index-news-issuances.html` | Formal agreements, cease-and-desist orders against bank officers/directors |
| `fed_enforcement` | Federal Reserve Enforcement | `https://www.federalreserve.gov/releases/enf/` | Prohibition orders against bank officers; structured HTML table, no RSS |
| `hhs_oig_alerts` | HHS OIG Fraud Alerts | `https://oig.hhs.gov/fraud/fraud-alerts/` | Healthcare fraud alerts; supplements the LEIE exclusions list with narrative context |
| `hud_enforcement` | HUD Enforcement | `https://www.hud.gov/program_offices/fair_housing_equal_opp/enforcement` | Fair housing enforcement; named respondents in discrimination cases |

**Scale:** ~50–200 new press releases/week across all sources combined. Each release yields 1–5 named individuals on average. Estimated ~5–20K new `individual_documents` rows/year for this subcategory.

**Implementation Notes:**
- Reuse the `doj_press` regex extraction pipeline; do not duplicate — refactor into a shared `AgencyPressScraper` base class parameterized by RSS URL and HTML listing URL
- Store raw HTML per press release in R2; confidence-flagged excerpts in PostgreSQL
- FDIC publishes a structured orders list (CSV/HTML table) in addition to press releases — prefer structured over free-text where available
- Federal Reserve enforcement page is HTML table only (no RSS); poll weekly and diff against stored content hash

#### 5b. Self-Regulatory Organization (SRO) Disciplinary Actions

| Source ID | Organization | URL | Notes |
|---|---|---|---|
| `finra_actions` | FINRA Disciplinary Actions | `https://www.finra.org/rules-guidance/oversight-enforcement/finra-disciplinary-actions` (monthly Excel) | Named brokers/advisors; monthly Excel download; ~200–400 actions/month; also BrokerCheck API |
| `finra_brokercheck` | FINRA BrokerCheck | `https://api.brokercheck.finra.org/` | Public REST API; individual broker registration, disclosure events, employment history |
| `nfa_actions` | NFA Disciplinary Actions | `https://www.nfa.futures.org/news/disciplinaryNotices.asp` | CFTC-registered futures professionals; HTML listing with downloadable notices |

**Scale:** ~3,000–5,000 FINRA disciplinary actions/year. BrokerCheck covers ~600K registered individuals.

**Implementation Notes:**
- FINRA monthly Excel is the primary bulk source; BrokerCheck API for incremental and detail enrichment
- BrokerCheck has a public REST API but no bulk download — implement as targeted lookups for known individuals, not full enumeration
- NFA actions are HTML-only; scrape listing page + parse individual notice pages

#### 5c. Court Records

| Source ID | Source | URL | Notes |
|---|---|---|---|
| `courtlistener` | CourtListener (RECAP) | `https://www.courtlistener.com/api/rest/v4/` | Free REST API over PACER data uploaded by RECAP; covers federal district/appellate courts; ~5M+ dockets |
| `pacer_rss` | PACER RSS Feeds | `https://ecf.{court}.uscourts.gov/cgi-bin/rss_outside.pl` | Each federal court publishes its own RSS of recent filings; ~200 courts; no fee for RSS |

**Scale:** CourtListener has 5M+ dockets and 80M+ documents; intake is selective — index only criminal cases and named defendants, not all filings.

**Implementation Notes:**
- CourtListener REST API: query `https://www.courtlistener.com/api/rest/v4/dockets/?nature_of_suit=&type=cr` for criminal cases; extract parties with `party_type=defendant`
- PACER RSS feeds are free and require no account — each federal court publishes a feed of recently-filed entries; parse for criminal docket entries naming defendants
- Do not download full PACER documents (fees); index docket entries and party names only
- CourtListener also has a bulk data download (`https://www.courtlistener.com/api/bulk-data/`) — use for initial historical load

#### 5d. News Wire RSS Feeds

General-purpose news coverage for named individuals. Unlike enforcement sources, news wires do not restrict to criminal/legal context — any newsworthy named individual may appear. Extraction is lower-confidence than enforcement sources; store with a `news_mention` relationship type and source confidence score.

| Source ID | Source | RSS URL | Notes |
|---|---|---|---|
| `reuters_rss` | Reuters | `https://feeds.reuters.com/reuters/topNews` + topic feeds | Multiple topic feeds (business, politics, legal/crime); no paywall for headlines |
| `ap_rss` | Associated Press | `https://rsshub.app/apnews/topics/apf-topnews` | AP top news + topic feeds; no paywall for headlines |
| `propublica_rss` | ProPublica | `https://www.propublica.org/feeds/propublica/main` | Investigative journalism; high individual-name density per article |
| `gdelt` | GDELT Project | `https://api.gdeltproject.org/api/v2/summary/summary` | Global news event database; covers 150+ countries; REST API for queries by person/theme |

**Scale:** ~500–2,000 relevant articles/day across all wires. Most articles name 0–2 individuals directly in the headline/lede. With selective filtering (legal/financial/political/business beats), reduce to ~50–200 processable articles/day.

**Implementation Notes:**
- Store full article text (headline + lede paragraph only, not full body — copyright) in R2; extract named entities from headline + first 2 paragraphs only
- For Reuters/AP, consume headline and summary only from RSS — do not fetch full article pages
- Named entity extraction for news sources is less deterministic than enforcement sources; store relationship as `news_mention` with a `confidence` field in `identifiers`
- GDELT provides a structured API for person mentions across global news — query by name for enrichment rather than bulk ingest; use as a supplement to direct RSS monitoring
- ProPublica articles tend to have higher individual-name density and more investigative context; worth fetching full article text (no paywall)

#### 5e. Additional Structured Sources

| Source ID | Source | URL | Access | Notes |
|---|---|---|---|---|
| `interpol_notices` | INTERPOL Red Notices | `https://ws-public.interpol.int/notices/v1/red` | REST API, no auth | Internationally wanted persons; ~7,000 active red notices; JSON API |
| `state_dept_designations` | State Dept. Terrorist Designations | `https://www.state.gov/bureau-of-counterterrorism/foreign-terrorist-organizations/` | HTML | Named individuals in FTO/SDGT designations; complements OFAC SDN |
| `usms_fugitives` | USMS Most Wanted | `https://www.usmarshals.gov/investigations/most_wanted/` | HTML | Named federal fugitives; small dataset (~100 active); HTML table scrape |
| `ice_most_wanted` | ICE Most Wanted | `https://www.ice.gov/most-wanted` | HTML | Named individuals; immigration/criminal enforcement context |
| `hhs_exclusions_alert` | HHS OIG Self-Disclosure | `https://oig.hhs.gov/compliance/self-disclosure-info/` | HTML | Complements LEIE with narrative context for fraud resolutions |

**Scale:** Small individually; combined ~2,000–5,000 new records/year.

---

### Phase 6 — Search Improvements + Deferred Sources

- `pg_trgm` fuzzy search tuning
- Deferred sources review: revisit out-of-scope list based on investigative priorities

---

## 11. Pre-Implementation Verification Checklist

Before writing code for these sources, make one manual request to verify access:

1. **ATF FFL** — GET `https://www.atf.gov/firearms/listing-federal-firearms-licensees`; confirm monthly files are directly downloadable without login; check `https://www.atf.gov/robots.txt`

2. **FDA Debarment** — Fetch the debarment page to find current XLSX download URLs; confirm whether OpenFDA covers all debarment categories or only drug product applications

3. **USASpending** — Verify current archive filenames on `https://www.usaspending.gov/download_center/award_data_archive`; naming convention includes date stamps that change

4. **CMS Open Payments** — Visit `https://openpaymentsdata.cms.gov/` to retrieve stable download URLs for PY2015–PY2024; not guaranteed to be at predictable paths

5. **EDGAR User-Agent email** — Decide on the email address to include in the required `User-Agent` header for all EDGAR requests before running any EDGAR scraper

6. **SAM.gov API key** — Register at `https://sam.gov/profile/details` before SAM scraper development begins

7. **LDA API key** — Register at `https://lda.senate.gov/api/register/` before LDA scraper development begins

---

## 12. Python Dependencies

```
httpx             # async HTTP client
playwright        # JS-rendered page scraping (install only if needed)
beautifulsoup4    # HTML parsing
polars            # fast CSV/bulk file parsing with lazy/streaming evaluation
lxml              # XML parsing
openpyxl          # FDA Debarment XLSX parsing
apscheduler       # job scheduling
boto3             # R2 access (S3-compatible endpoint)
psycopg2-binary   # PostgreSQL driver (sync, for bulk COPY operations)
asyncpg           # PostgreSQL driver (async, for normal scraper inserts)
tenacity          # retry logic with exponential backoff
structlog         # structured logging
fastapi           # query API
uvicorn           # ASGI server for FastAPI
```
