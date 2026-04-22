# PubRecSearch — Implementation Plan

## 1. System Overview

PubRecSearch is a batch scraping pipeline, datalake, and search interface for U.S. federal public records. The system continuously ingests records from ~25 federal sources, extracts named individuals, stores raw source files in Cloudflare R2, and provides a queryable index linking individuals to their source documents.

The system is **not** a real-time service. It is a scheduled batch pipeline with a read interface. Architecture decisions should favor operational simplicity and cost over latency and throughput.

---

## 2. Data Scale Analysis

Research into published statistics for each source reveals that the original infrastructure estimates were significantly understated. This section documents findings per source and derives aggregate storage and database requirements that must inform infrastructure choices.

### Per-Source Scale Estimates

**In-scope sources:**

| Source | Total Records (Current) | New Records / Year | Raw File Size / Year | DB Rows / Year | Notes |
|---|---|---|---|---|---|
| **FEC Individual Contributions** | ~300M+ (full history 1979–) | ~50–100M contribution records | ~4–5 GB/cycle compressed | ~50–100M | indiv{year}.zip; full history ~40–60 GB compressed; dominant row-count source |
| **SEC EDGAR** | Millions of filings (1993–) | ~500K individual-relevant | ~10–15 GB/year (Form 4/3/SC13G only) | ~500K–1M | Form 4 alone ~1.5M filings/year; scoping to individual-relevant filings is essential |
| **USASpending.gov** | FY2001–present | Millions of award actions/year | ~5–15 GB/year compressed | ~1–2M | Individual recipients are a small fraction; filter aggressively at parse time |
| **IRS Form 990** | 3M+ tax returns; 9.6M+ filing documents | ~150K–200K filings | ~5 GB/year compressed (~17 GB uncompressed) | ~2–4M (officers/directors per filing) | IRS EFILE bulk XML on AWS S3; each filing 20–500 KB; officer/director fields are the extraction target |
| **DOJ Press Releases** | Years of archives | ~3K–8K releases | ~0.5–1 GB/year (HTML) | ~10K–30K | NLP extraction; named defendant count per release averages 1–3; confidence scoring needed |
| **OFAC SDN List** | ~15,000 total entries | Hundreds/year | ~20 MB (full XML, replaced each update) | ~5K individuals | Small; full list replaced on each update |
| **ATF FFL** | **128,690** active licensees (FY2024) | ~44K issued/year (incl. renewals) | ~5–10 MB/month CSV | ~130K (stable) | Monthly CSV; sole proprietors identified by license type + name pattern |
| **NSOPW** | ~850,000 registered (all states) | ~10K–15K/year net | N/A | — | **Excluded — ToS prohibits bulk enumeration** |
| **BOP Inmate Locator** | ~157K–170K current federal inmates | ~20K–30K new admissions/year | N/A — no bulk download | ~160K (active) | **Out of scope — no bulk download; enumeration approach raises ToS concerns** |
| **LDA Lobbying** | 726K+ cumulative filings (2018–) | ~55K filings/year | ~50–100 MB/year | ~100K–200K lobbyist mentions | Quarterly LD-1/LD-2/LD-203 reports; lobbyist names extracted per filing |
| **FARA** | ~500 active registrants | ~1K–2K filings/year | ~10–20 MB/year | ~2K–5K | Small; semi-annual supplemental statements; bulk XML available |
| **FCC ULS** | Millions of licenses | ~100K+/year individual licenses | ~1–3 GB/year (weekly incrementals) | ~1–2M individuals | **Out of scope** |
| **CMS Open Payments** | **84M** cumulative (2013–2024) | **~16M/year** (record high 2024) | ~3–5 GB/year compressed | ~16M/year | Annual program year files; ~1.5M unique covered recipients; second-largest row source |
| **OIG Exclusions** | **~78,927** total | ~3K–4K/year | ~30 MB (monthly CSV) | ~79K (initial); ~4K/year | Monthly full-replacement CSV |
| **FDA Debarment** | ~100–200 total | Handful/year | <1 MB | ~200 (stable) | Static HTML table; rarely changes |
| **SAM.gov Exclusions** | Unknown (daily refresh) | Hundreds–thousands/year | ~10–20 MB (daily CSV) | ~10K–50K individuals | Covers procurement debarments; separate from OIG LEIE |
| **EPA ECHO** | 800K+ regulated facilities | ~10K–20K enforcement actions/year | ~1–2 GB/year (API export) | ~20K–50K named individuals/year | Defendants in enforcement actions; volume varies significantly by year |

**Out-of-scope sources (deferred indefinitely):**

| Source | Reason for Deferral |
|---|---|
| Congressional Record | Free-text individual extraction too complex for current scope; structured member data adds little not covered by FEC/LDA |
| FAA Airmen (~904K records) | Licensing database; investigative value lower than other sources in scope |
| NPPES NPI Registry (5.5M records, 9.3 GB/mo) | Healthcare provider licensing; limited investigative relevance relative to bulk |
| USPTO Patents (~350K grants/year) | IP ownership; deferring IP data sources as a category |
| USPTO Trademarks (~767K apps/year) | IP ownership; same as above |
| Copyright Office (~500K/year) | IP ownership; same as above |
| House Financial Disclosures (~2–3K/year) | PDF/XML complexity; revisit if PDF extraction approach is resolved |
| Senate Financial Disclosures (~400/year) | PDF-only; high extraction complexity for low volume |
| OGE Financial Disclosures (~25–30K/year) | Mostly PDFs; similar to Senate disclosures |
| NTSB Accidents (~1,500–2,000/year) | Small dataset; named individuals are crew members, limited investigative scope |
| CPSC Recalls (~500/year) | Named individuals extremely rare; primarily company/product data |

### Aggregate Estimates (In-Scope Sources, 2015 Cutoff)

#### Raw File Storage

Annual ongoing storage and historical backfill estimates for in-scope sources only, starting from 2015:

| Source | Annual Ongoing | 2015–2024 Backfill |
|---|---|---|
| IRS Form 990 | ~5 GB/year compressed | ~40–50 GB |
| SEC EDGAR (Form 4/3/SC13G) | ~10–15 GB/year | ~80–120 GB |
| USASpending | ~5–10 GB/year | ~40–80 GB |
| FEC | ~4–5 GB/cycle (2-year) | ~25–35 GB |
| CMS Open Payments | ~3–5 GB/year | ~20–35 GB |
| ~~FCC ULS~~ | ~~out of scope~~ | ~~out of scope~~ |
| DOJ Press Releases | ~0.5–1 GB/year | ~5–8 GB |
| EPA ECHO | ~1–2 GB/year | ~8–15 GB |
| All small sources combined | <0.5 GB/year | ~2 GB |
| **Total** | **~30–50 GB/year** | **~225–355 GB** |

| Horizon | Estimated Total Storage | R2 Cost/mo |
|---|---|---|
| Historical backfill complete (2015–present) | ~225–355 GB | ~$3.50–5.50 |
| Year 2 (backfill + ongoing) | ~275–410 GB | ~$4.00–6.00 |
| Year 5 (ongoing accumulation) | ~375–560 GB | ~$5.50–8.50 |

Dominant consumers after removing out-of-scope sources: **EDGAR** (~10–15 GB/year), **IRS 990** (~5 GB/year compressed), **USASpending** (~5–10 GB/year). NPPES removal eliminates the previous largest single-file concern (9.3 GB/month).

#### Database Row Counts

Note on granularity: the `individual_documents` table schema has a composite PK on `(individual_id, document_id, relationship)`. This means one row per person per bulk file, not one row per transaction. For FEC, a person appearing in the 2023 annual bulk file gets one linkage row regardless of how many donations they made that year. The identifiers/excerpt fields capture per-transaction detail as JSON. This keeps the row count manageable but limits per-transaction querying.

| Table | Estimated Rows (2015–present, in-scope only) |
|---|---|
| `individuals` (unique persons) | ~15–40M |
| `individual_documents` (linkages) | ~60–120M |
| `documents` (file metadata) | ~500K–2M |
| `scrape_jobs` / `scrape_errors` | Negligible |

**FEC unique donors (2015–2024)** and **Open Payments covered recipients** (~1.5M cumulative unique) drive the individuals count. The `individual_documents` row count is much more manageable than originally estimated once NPPES (5.5M records) and the IP/disclosure sources are removed.

#### Infrastructure Implication (Revised)

Without NPPES, the largest single file becomes EDGAR quarterly index files (~1–3 GB each) and IRS 990 bulk XMLs. These are manageable with Polars streaming on a machine with 4+ GB RAM. The database scale (60–120M `individual_documents` rows) still requires PostgreSQL but is comfortably within reach of a well-provisioned local machine or a mid-tier VPS.

**D1 is still not viable** — FEC alone produces ~10–20M unique donors across 5 cycles, and the `individual_documents` table will exceed D1's 10 GB limit well before full load. PostgreSQL remains required. See Q1 in Section 9.

---

## 3. Infrastructure Stack

### Compute — Scraper + Database Host

**Current plan: Local hardware**

The system runs on local hardware rather than a cloud VPS. This eliminates the largest recurring cost (~$48/mo for a cloud equivalent) at the tradeoff of availability risk and manual maintenance. Key requirements for the local machine:

- **RAM: 8 GB minimum, 16 GB recommended** — PostgreSQL `shared_buffers` (2 GB), peak scraper working memory for large bulk files (2–4 GB with Polars streaming), OS + headroom
- **Storage: 500 GB+ available** — PostgreSQL data directory will grow to ~100–300 GB over time; temp workspace needed for in-progress downloads (largest single file after removing NPPES: EDGAR quarterly index ~3 GB compressed → ~10 GB uncompressed)
- **Always-on during scrape windows** — scrapers run on cron; machine must be up during scheduled windows or jobs will be missed (APScheduler will catch up on next run if `coalesce=True`, but hours-long jobs cannot be split)
- **Residential internet bandwidth note** — historical backfill of ~225–355 GB will take hours to days depending on connection speed; this is a one-time cost

**VPS fallback (if local hardware is unavailable or unreliable)**
- Vultr Cloud Compute 4 GB (2 vCPU, 80 GB SSD): ~$24/mo — adequate for in-scope sources after NPPES removal; serial scraper scheduling keeps peak memory manageable
- Vultr 8 GB (4 vCPU, 160 GB SSD): ~$48/mo — comfortable headroom

### Storage

| System | Purpose | Cost |
|---|---|---|
| Local disk | PostgreSQL primary data store, temp download workspace | $0 (hardware already owned) |
| Cloudflare R2 | Raw source file archive; periodic PostgreSQL dump backups | $0.015/GB/mo |

**R2 serves two roles in the local hardware model:**
1. **Raw file archive** — every downloaded source file is uploaded to R2 before processing. This preserves originals independently of local disk, and allows re-parsing if logic changes without re-downloading from sources.
2. **Database backup** — `pg_dump` runs on a schedule and uploads compressed dumps to R2. At ~100–300 GB database size, a compressed dump will be 20–80 GB; stored in a separate R2 bucket/prefix with a retention policy (e.g., 30 days of daily dumps).

**PostgreSQL replaces D1.** D1 is not viable at this scale — see Q1 in Section 9.

### API / Query Layer

**Undecided.** Options:

- **Local FastAPI process** — query API runs on the same machine as PostgreSQL; accessible only on local network or via VPN. Zero external cost; suitable if this is a single-user tool.
- **Cloudflare Workers + Tunnel** — a Cloudflare Tunnel exposes the local PostgreSQL (or a local API process) to a Worker without opening a public port. Gives a public HTTPS endpoint with Cloudflare's access controls. Free tier sufficient.
- **VPS as API host only** — a minimal VPS (Vultr 1 GB, $3.50/mo) runs only the query API; connects to local PostgreSQL via Cloudflare Tunnel. Separates API availability from local machine uptime.

### Monitoring

Structured logs via `structlog` to local log files + rotation. Email alerts via **Resend** (free tier: 3,000 emails/mo) on scrape failures. A daily summary email from the scraper provides a heartbeat confirming the machine and jobs are running.

### Estimated Monthly Cost

| Component | Cost |
|---|---|
| Local hardware | $0 (existing) |
| R2 raw file archive (~300 GB at backfill complete) | ~$4.50 |
| R2 DB backups (~30 days × ~30 GB compressed dump) | ~$1.50 |
| R2 ongoing (annual growth ~35 GB) | +~$0.50/mo/year |
| Email alerts (Resend free tier) | $0 |
| **Total** | **~$6–8/mo** |

R2 is essentially the entire recurring cost. If the query API needs external access, add ~$3.50–48/mo for a VPS depending on approach chosen.

---

## 4. Data Architecture

### Database Schema (PostgreSQL)

```sql
-- Named individuals extracted from any source
CREATE TABLE individuals (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,      -- normalized: lowercase, stripped punctuation
    aliases     JSONB,              -- array of alternate name strings
    merge_group_id BIGINT,          -- nullable; for future entity resolution grouping
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Full-text search index
CREATE INDEX individuals_fts_idx ON individuals USING GIN (to_tsvector('english', name));
-- Trigram index for fuzzy name matching
CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);

-- Every raw file stored in R2
CREATE TABLE documents (
    id          BIGSERIAL PRIMARY KEY,
    r2_key      TEXT NOT NULL UNIQUE,
    source_id   TEXT NOT NULL,      -- e.g. "fec_donors", "ofac_sdn"
    source_url  TEXT,
    captured_at TIMESTAMPTZ NOT NULL,
    file_hash   TEXT NOT NULL,      -- SHA-256, used for dedup
    file_size   BIGINT,
    doc_type    TEXT,               -- "csv", "xml", "pdf", "html"
    period      TEXT                -- e.g. "2024-Q1", "2025-04-17"
);
CREATE INDEX documents_source_idx ON documents (source_id, captured_at DESC);

-- Individual ↔ document linkage with role context
CREATE TABLE individual_documents (
    individual_id   BIGINT NOT NULL REFERENCES individuals(id),
    document_id     BIGINT NOT NULL REFERENCES documents(id),
    relationship    TEXT NOT NULL,  -- "defendant", "donor", "licensee", "registrant", etc.
    excerpt         TEXT,           -- relevant snippet from document
    identifiers     JSONB,          -- {"npi": "...", "fec_id": "...", "ein": "..."}
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

-- Structured error log
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
  raw/fec_donors/2024-Q1/indiv24.zip
  raw/ofac_sdn/2025-04-17/sdn_xml.zip
  raw/nppes_npi/2025-04/npidata_pfile_20250401-20250407.csv
  raw/doj_press/2025-04-17/pr-2025-04-17-143022.html
  raw/sec_edgar/2025-04/0001234567-25-000001-index.json
```

### Name Normalization

All names stored in `name_norm` must be processed identically at ingest and at query time:
1. Unicode NFKD normalization, strip combining characters (accents)
2. Lowercase
3. Strip punctuation except hyphens and apostrophes within words
4. Collapse whitespace
5. For organizations: strip common suffixes (LLC, Inc, Corp, Co, Ltd) into a separate field

This is the most impactful data quality decision. It must be centralized in a single function used by all scrapers and by the search Worker.

---

## 5. Scraper Architecture

### Module Interface

Every data source implements a Python class conforming to:

```python
class BaseScraper:
    source_id: str          # snake_case identifier, matches scrape_state.source_id in PostgreSQL
    schedule: str           # cron expression, e.g. "0 2 * * 1" (weekly Monday 2am)
    doc_type: str           # default doc_type for documents from this source

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

A simple Python scheduler on the VPS using APScheduler. On each run:

1. Load `scrape_state` for the source from PostgreSQL
2. Call `discover()` — returns only new/changed targets based on state
3. Insert a `scrape_jobs` row with status `running`
4. For each target: fetch → parse → store, catching and logging errors per-record
5. Update `scrape_state` cursor on success
6. Update `scrape_jobs` row to `success` or `partial`/`failed`
7. POST completion event to the Cloudflare Worker monitoring endpoint

### Error Handling Policy

- **HTTP 4xx (not 429)**: Log as error, mark target as failed, continue with remaining targets
- **HTTP 429 / rate limit**: Exponential backoff up to 3 retries, then log and skip
- **HTTP 5xx**: Retry up to 3 times with backoff, then log as error
- **Parse errors on a record**: Log as warning with raw excerpt, skip record, continue
- **Parse errors on entire file**: Log as critical, mark job `partial`, do not update cursor (will retry next run)
- **R2 upload failure**: Retry 3 times, then mark job `failed` and halt — do not write PostgreSQL records without the corresponding R2 file

A job completes as `partial` if >0 records succeeded and >0 failed. The cursor advances only to the last fully-successful point.

---

## 6. Data Source Implementation

### Source Classification

| Type | Approach | Sources |
|---|---|---|
| **Bulk file download** | HTTP GET with ETag/Last-Modified dedup | FEC, OFAC SDN, ATF FFL, NPPES NPI, OIG Exclusions, SAM.gov, NTSB, CPSC |
| **Paginated API** | Cursor/offset pagination, store max seen ID | SEC EDGAR, USASpending, ProPublica (990s), FCC ULS, Open Payments |
| **HTML scraping** | httpx + BeautifulSoup; Playwright only if JS required | DOJ Press Releases, BOP Inmate Locator, NSOPW, FARA, LDA |
| **Structured download** | File download + XML/CSV parse | USPTO Patents/Trademarks, Copyright Office, House/Senate/OGE Disclosures |
| **Exclude** | Requires account + per-page fees | PACER |

### Per-Source Details

#### Federal Financial & Tax

**IRS Form 990** (`irs_990`)
- Source: ProPublica Nonprofit Explorer API (`https://projects.propublica.org/nonprofits/api/v2/`)
- Approach: Paginated API by organization; individuals extracted from officer/director fields and Schedule L/O
- Relationship types: `officer`, `director`, `highly_compensated_employee`, `contractor`
- Incremental: store max `updated` timestamp from API; query `?updated_since=`
- Notes: API is free, no key required; rate limit ~1 req/sec; full 990 XML also available per-filing

**SEC EDGAR** (`sec_edgar`)
- Source: EDGAR Full-Text Search API and company search (`https://efts.sec.gov/LATEST/search-index`); bulk index at `https://www.sec.gov/Archives/edgar/full-index/`
- Approach: Pull quarterly index files (company.idx, crawler.idx); filter by form type for individual-relevant filings (4, 3, SC 13G/D, DEF 14A, 8-K, 10-K)
- Relationship types: `insider`, `beneficial_owner`, `executive`, `director`
- Incremental: quarterly index files are append-only; track last-processed quarter + offset
- Notes: EDGAR enforces 10 req/sec; User-Agent header with contact email required

**USASpending.gov** (`usaspending`)
- Source: USASpending API (`https://api.usaspending.gov/api/v2/`)
- Approach: Download awards bulk data files (contracts + grants); filter for individual recipients (business_types includes "individual")
- Relationship types: `contract_recipient`, `grant_recipient`
- Incremental: awards have `last_modified_date`; store cursor by fiscal year
- Notes: Bulk download files are large (multi-GB for full history); parse incrementally by fiscal year

#### Legal & Law Enforcement

**DOJ Press Releases** (`doj_press`)
- Source: `https://www.justice.gov/news` (HTML) + RSS feeds per component office
- Approach: httpx + BeautifulSoup; extract named defendants from press release text using regex patterns for "United States v.", "defendant [Name]", "charged with"
- Relationship types: `defendant`, `subject`, `victim` (when named)
- Incremental: store last-seen publication date; RSS feeds provide `pubDate`
- Notes: NLP-based name extraction will produce false positives; store raw HTML and excerpt, flag confidence level

**PACER** (`pacer`) — **EXCLUDED**
- Requires free account + $0.10/page fee for documents; no bulk access
- Mark as manual-only import if needed for specific cases

**OFAC SDN List** (`ofac_sdn`)
- Source: `https://www.treasury.gov/ofac/downloads/sdn_xml.zip`
- Approach: Bulk XML download; parse `<sdnEntry>` elements for individuals (type=`Individual`)
- Relationship types: `sanctioned_individual`
- Incremental: ETag/Last-Modified on the ZIP; file changes when list is updated
- Notes: Also available as CSV (`sdn.csv`); XML preferred for structured aliases and identifiers

**ATF FFL List** (`atf_ffl`)
- Source: `https://www.atf.gov/firearms/listing-federal-firearms-licensees` (monthly CSV downloads)
- Approach: Bulk CSV download; sole proprietors identified by license type + business name matching individual name patterns
- Relationship types: `ffl_licensee`
- Incremental: Monthly files; store last-downloaded month

**NSOPW** (`nsopw`)
- Source: `https://www.nsopw.gov/` — individual state registry APIs aggregated by NSOPW
- Approach: NSOPW provides a public API (`https://www.nsopw.gov/api/`) for searching; no bulk download
- Relationship types: `sex_offender_registrant`
- Incremental: API supports search but not bulk download; schedule frequent full sweeps by jurisdiction
- Notes: Coverage and data freshness varies by state; some states block bulk queries; flag per-state availability

**BOP Inmate Locator** (`bop_inmates`) — **OUT OF SCOPE**
- Incremental: Track last-enumerated register number range; BOP numbers are sequential
- Notes: No official bulk download; enumeration by register number is the only viable approach; respect rate limits aggressively

#### Elections & Political Finance

**FEC** (`fec`)
- Source: Bulk data downloads at `https://www.fec.gov/data/browse-data/?tab=bulk-data`
- Approach: Download individual contributions (indiv{year}.zip), candidate master, committee master files
- Relationship types: `donor`, `candidate`, `committee_treasurer`
- Incremental: Quarterly bulk files; FEC also provides an API for incremental updates (`https://api.open.fec.gov/v1/`)
- Notes: Annual individual contribution files are large (1–3GB compressed); parse in streaming fashion

**Lobbying Disclosure Act** (`lda`)
- Source: `https://lda.senate.gov/api/` (REST API)
- Approach: Paginated API for filings; extract lobbyists (individual names) and clients
- Relationship types: `lobbyist`, `registrant_contact`
- Incremental: API supports `filing_dt_posted__gte` date filter; store last run timestamp

**FARA** (`fara`)
- Source: `https://efile.fara.gov/bulk-data/` (bulk XML/CSV downloads available)
- Approach: Bulk download of registrant and exhibit files; extract individual foreign agents
- Relationship types: `foreign_agent`, `short_form_registrant`
- Incremental: Bulk files updated periodically; ETag dedup

#### Professional Licensing & Credentials

**FAA Airmen** (`faa_airmen`) — **OUT OF SCOPE**

**FCC License Search** (`fcc_uls`) — **OUT OF SCOPE**

**NPPES NPI Registry** (`nppes_npi`) — **OUT OF SCOPE**

#### Healthcare

**CMS Open Payments** (`cms_open_payments`)
- Source: `https://openpaymentsdata.cms.gov/` — bulk CSV downloads per program year
- Approach: Download General Payments, Research Payments, and Ownership data; extract covered recipients (physicians/non-physician practitioners)
- Relationship types: `payment_recipient`, `ownership_interest_holder`
- Incremental: Annual program year files; new year added each June; track downloaded years

**OIG Exclusions** (`oig_exclusions`)
- Source: `https://oig.hhs.gov/exclusions/exclusions_list.asp` (downloadable CSV/SAS)
- Approach: Bulk CSV download of full exclusions list
- Relationship types: `oig_excluded_individual`
- Incremental: File updated frequently; ETag/Last-Modified dedup; also offers an API (`https://oig.hhs.gov/exclusions/docs/LEIE_API_USER_GUIDE.pdf`)

**FDA Debarment List** (`fda_debarment`)
- Source: `https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/debarment-list`
- Approach: HTML scrape; page is relatively static with a structured table
- Relationship types: `fda_debarred_individual`
- Incremental: Page rarely changes; store hash of page content; re-parse only on change

#### Property, Intellectual & Business

**USPTO Patents** (`uspto_patents`) — **OUT OF SCOPE**

**USPTO Trademarks** (`uspto_trademarks`) — **OUT OF SCOPE**

**Copyright Office** (`copyright_office`) — **OUT OF SCOPE**

**SAM.gov Exclusions** (`sam_exclusions`)
- Source: `https://sam.gov/` — exclusions extractable via SAM.gov API (free, API key required) or bulk download
- Approach: API with `exclusionType=Individual`; also bulk CSV available on Data.gov
- Relationship types: `sam_excluded_individual`
- Incremental: API supports `updatedDate` filter; store last run timestamp

#### Congressional & Executive Records

**Congressional Record** (`congressional_record`) — **OUT OF SCOPE**

**House Financial Disclosures** (`house_disclosures`) — **OUT OF SCOPE**

**Senate Financial Disclosures** (`senate_disclosures`) — **OUT OF SCOPE**

**OGE Financial Disclosures** (`oge_disclosures`) — **OUT OF SCOPE**

#### Environment & Safety

**EPA ECHO** (`epa_echo`)
- Source: ECHO REST API (`https://echo.epa.gov/tools/web-services`)
- Approach: Query enforcement actions; extract named individuals in penalties and enforcement
- Relationship types: `epa_enforcement_subject`
- Incremental: API supports date-range filters; store last run timestamp

**NTSB Accident Database** (`ntsb_accidents`) — **OUT OF SCOPE**

**CPSC Recall Database** (`cpsc_recalls`) — **OUT OF SCOPE**

---

## 7. Query API (Cloudflare Workers)

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
     → Worker liveness check
```

### Search Behavior

Search queries are normalized identically to ingest (same normalization function, shared as a module). PostgreSQL `pg_trgm` supports fuzzy name matching (`similarity(name_norm, $query) > 0.4`); `tsvector` full-text search handles tokenized name queries. Results ranked by similarity score with a secondary sort on most-recent document capture date.

---

## 8. Monitoring

### Scrape Health Alerts

A Cloudflare Worker exposes a webhook endpoint that receives POST events from the scraper on job completion. Alert conditions:

- Job status is `failed` or `partial` with `errors_count > threshold`
- Source has not had a `success` run in more than `N` days (configurable per source)
- `records_new == 0` for a source that normally produces new records (potential silent failure)

Alerts delivered via Resend email to a configured address.

### Scrape Dashboard

The `/sources` endpoint returns enough data to build a simple status dashboard: last run time, success/fail, record counts, error counts. A static HTML page served from R2 (or a Worker) can poll this endpoint.

### Audit Log

Every `scrape_jobs` row is a permanent audit record. The `scrape_errors` table retains per-record error context. Neither table is ever truncated — they are the auditability mechanism.

---

## 9. Design & Architectural Questions

### Q1: D1 vs. PostgreSQL for the primary database

**Resolved: PostgreSQL (self-hosted on VPS)**

The scale analysis (Section 2) makes this decision clear. D1 is eliminated:

- **D1 storage limit (10 GB)**: NPPES NPI alone is 9.3 GB uncompressed as a single file. The full database would exhaust D1's limit before even loading FEC or Open Payments.
- **D1 row capacity**: At ~350 bytes/row average, 10 GB ≈ 28M rows. The `individual_documents` table alone will reach 200–300M rows in year one.
- **D1 write mechanism**: No bulk insert / COPY support. Loading 100M FEC rows via the D1 HTTP API (1MB per request) would take days and is architecturally impractical.
- **D1 FTS5**: Only works on a single virtual table; cross-table full-text queries against linked documents are not possible.

PostgreSQL self-hosted on the VPS is the correct choice:
- No additional monthly cost (runs on the same VPS)
- Direct `psycopg2`/`asyncpg` connection from scrapers — no HTTP overhead for bulk inserts
- `COPY` command for bulk loads: 100M FEC rows loads in minutes, not days
- `pg_trgm` + GIN index for full-text and trigram search across name variants
- No size limits relevant to this workload
- Daily `pg_dump` to R2 for backup

The `individuals_fts` FTS5 virtual table in the schema is replaced with a PostgreSQL GIN index on `to_tsvector('english', name || ' ' || coalesce(aliases, ''))` and a `pg_trgm` GIN index on `name_norm` for fuzzy matching. 

---

### Q2: Individual entity resolution strategy

**Impact:** This is the hardest data quality problem in the system. The same person appears across sources with name variations, middle name presence/absence, suffixes, typos, and aliases. Without entity resolution, search returns fragmented results for the same person.

**Option A — No resolution at ingest (store as-is, resolve at query time)**
- Each source record creates a new `individual` row
- Full-text search returns all name matches; UI groups likely matches
- Pros: Simple, no false merges, reversible
- Cons: Search results are noisy; a query for "John Smith" returns 40 fragmented records

**Option B — Deterministic matching at ingest**
- Use composite keys: if source provides a unique identifier (NPI, FEC ID, EIN, SSN-derived hash), merge on that
- For sources without IDs, match on normalized name + date of birth (when available) + state
- Pros: Structured sources (NPPES, FEC) get clean merges; reduces duplication significantly
- Cons: Still misses cross-source merges for sources without shared identifiers

**Option C — Probabilistic entity resolution**
- Dedupe library (e.g., `splink`, `recordlinkage`) run as a periodic batch job
- Candidates scored by name similarity + available attributes; above threshold → merge
- Pros: Best recall across sources without shared IDs
- Cons: Significant complexity; false merges are harmful in investigative contexts; requires human review pipeline

**Recommendation:** Implement Option B at ingest (deterministic on available identifiers), store a `source_identifier` field in `individual_documents` for all source-native IDs, and defer probabilistic resolution. Add a `merge_group_id` nullable column to `individuals` for future Option C implementation without schema migration.

**Decision needed:** Define acceptable false merge rate — investigative use favors under-merging (fragmented results) over over-merging (conflating two different people).

Answer: Option A. Do not match initially. This is a substantially different component of a robust datasystem, whereas this particular project focuses on the data acquisition, not analysis. That will be a downstream component I develop separately. 

---

### Q3: PDF handling for financial disclosures

**Resolved: Not applicable.** House, Senate, and OGE financial disclosures are all out of scope. PDF handling is not required for any in-scope source. DOJ press releases are HTML, not PDF. `pdfplumber` is removed from dependencies. 

---

### Q4: NSOPW bulk access approach

**Impact:** NSOPW aggregates state sex offender registries but does not provide bulk download. Individual state registries vary in accessibility.

**Option A — NSOPW API search-based enumeration**
- Query NSOPW API by first name initial + state; page through results
- Estimated coverage: ~60–70% of registrants (API may throttle or block systematic enumeration)
- Pros: Single integration point
- Cons: Incomplete coverage, rate limiting risk, may violate ToS

**Option B — Direct state registry scrapers**
- Build scrapers for each state's public registry (50+ scrapers)
- Pros: Complete coverage, fresher data
- Cons: Substantial build effort; state sites change frequently; some states actively block scraping

**Option C — Third-party data aggregators**
- Commercial providers aggregate NSOPW/state data (not free)
- Excluded by project scope

**Recommendation:** Option A (NSOPW API) for initial implementation with explicit coverage tracking per state. Document known gaps. Only build Option B scrapers for states where NSOPW API consistently returns zero or incomplete results.

ANSWER: Skip. It violates the TOS to scrape. 

---

### Q5: Scraper deployment model — single VPS process vs. containerized jobs

**Impact:** Affects operational complexity, failure isolation, and resource contention.

**Option A — Single Python process, APScheduler**
- One process manages all scraper schedules; scrapers run in sequence or threads
- Pros: Simplest deployment; no orchestration overhead
- Cons: One crashing scraper can affect others; no resource isolation; harder to run scrapers in parallel for large downloads

**Option B — Separate process per scraper (systemd services or Docker containers)**
- Each scraper is an independent service with its own schedule (cron or systemd timer)
- Pros: Full isolation; easy to restart/redeploy individual scrapers; parallelism is natural
- Cons: More systemd unit files / Docker configs to manage

**Option C — Containerized with Docker Compose on VPS**
- Each scraper as a Docker service; shared volumes for temp storage; Compose for orchestration
- Pros: Reproducible; easy to add new scrapers; resource limits per container
- Cons: Docker overhead on 1GB RAM VPS may be tight; adds deployment complexity

**Recommendation:** Option A (single process, APScheduler) for initial implementation. APScheduler supports job isolation with thread pools and per-job error handling. If memory pressure or scraper interference becomes a problem, migrate to Option B (systemd timers — simpler than Docker on a small VPS).

Answer: Decide later, after scale questions are checked. x

---

### Q6: Initial data load strategy for large bulk sources

**Impact:** The scale analysis reveals the first-run problem is more severe than originally estimated. Full historical loads for the largest sources:

| Source | Full history size | Practical cutoff recommendation |
|---|---|---|
| FEC individual contributions | ~40–60 GB compressed (1979–) | 2010 (6 cycles); ~20 GB |
| SEC EDGAR (individual filings) | Multi-TB for all filings; ~100 GB for Form 4/3 only | 2010 for Form 4; skip historical for 10-K/8-K |
| IRS 990 | ~200 GB XML (2001–) | 2015; ~75 GB |
| CMS Open Payments | 2013–; ~40–50 GB compressed | No cutoff; start from 2013 (manageable) |
| NPPES NPI | ~10 GB/month; only current snapshot needed | Always current; no history |
| Copyright Office | 22M records; bulk catalog available | Start from 2000; catalog download |
| USASpending | FY2001–; ~100+ GB compressed full history | FY2015; ~30 GB |

**Key constraints with the 8 GB VPS:**
- 160 GB local SSD: sufficient for temp workspace (download + decompress + parse + upload to R2, then delete)
- 8 GB RAM: sufficient for streaming parse of any single file with Polars lazy evaluation
- PostgreSQL `COPY` bulk load: FEC year file (~50M rows) takes ~10–20 minutes with proper indexing deferred until after load

**Recommendations:**
1. Define a historical cutoff date before starting (decision needed)
2. Write a one-time `bootstrap.py` script per large source that: streams download → writes to R2 → streams parse → bulk COPY to PostgreSQL → deletes temp file. Never holds an entire uncompressed file in memory.
3. Process large historical loads one year at a time, not all at once, to avoid filling local disk
4. Defer index creation until after bulk load for each source (drop indexes → COPY → recreate indexes)
5. Incremental scrapers take over automatically after bootstrap completes

**Resolved: 2015 as the historical cutoff for all sources.**

Data prior to 2015 is excluded from initial load. This covers approximately one decade of federal records and captures the period of most interest for most investigative purposes. Sources with natural annual or quarterly file boundaries (FEC, EDGAR, IRS 990, USASpending, Open Payments) will be bootstrapped year-by-year from 2015 forward.

**Still open: automated vs. manual bootstrap.** The one-time bootstrap scripts should be written as standalone Python scripts (not part of the recurring scheduler) that process one year at a time, verify R2 upload + PostgreSQL load, and log completion. Whether these are run manually or as a one-shot automated sequence is a runtime decision.

---

## 10. Implementation Phases

### Phase 1 — Foundation (core infrastructure + small bulk sources)
- Local machine setup: PostgreSQL install, schema creation, R2 bucket, Cloudflare credentials
- Job runner, base scraper interface, R2 upload + PostgreSQL write utilities, Polars streaming helpers
- Name normalization function (shared between scraper and query API)
- Scrapers: OFAC SDN, ATF FFL, OIG Exclusions, SAM.gov Exclusions, FDA Debarment
- Query API (`/search`, `/individual`, `/sources`) — FastAPI locally; deployment approach TBD
- Basic email monitoring alerts (Resend) + daily heartbeat email

### Phase 2 — Large bulk sources + historical backfill (2015–present)
- FEC (current cycle first, then historical bootstrap 2015–2024)
- IRS Form 990 (via IRS EFILE bulk XML on AWS S3, 2015–present)
- CMS Open Payments (2015–present)
- SEC EDGAR (Form 4/3/SC13G, 2015–present)
- USASpending.gov (FY2015–present, individual recipients only)
- Historical bootstrap scripts (standalone, run year-by-year)
- Scrape health dashboard

### Phase 3 — Structured API sources
- LDA Lobbying Disclosure (API)
- FARA (bulk XML)
- ~~FCC ULS~~ — out of scope

### Phase 4 — HTML scraping sources (higher maintenance)
- DOJ Press Releases (NLP defendant extraction)
- ~~BOP Inmate Locator~~ — out of scope
- EPA ECHO (enforcement actions API)

### Phase 5 — Entity resolution + search improvements
- Deterministic merge on source-provided identifiers (FEC ID, NPI cross-reference if NPPES added later, EIN)
- `pg_trgm` fuzzy search tuning
- Deferred sources review: revisit out-of-scope list based on investigative priorities

**Excluded indefinitely:** NSOPW (ToS), PACER (fees), BOP Inmate Locator (no bulk download / ToS concerns), FCC ULS (Ham radio; low investigative value), Congressional Record, FAA Airmen, NPPES NPI, USPTO Patents/Trademarks, Copyright Office, House/Senate/OGE Disclosures, NTSB, CPSC

---

## 11. Python Dependencies

```
httpx          # async HTTP client
playwright     # JS-rendered page scraping (install only if needed)
beautifulsoup4 # HTML parsing
polars         # fast CSV/bulk file parsing with lazy/streaming evaluation
lxml           # XML parsing
apscheduler    # job scheduling
boto3          # R2 access (S3-compatible endpoint)
psycopg2-binary  # PostgreSQL driver (sync, for bulk COPY operations)
asyncpg        # PostgreSQL driver (async, for normal scraper inserts)
tenacity       # retry logic with exponential backoff
structlog      # structured logging
openpyxl       # FDA Debarment XLSX parsing
fastapi        # query API (if served from VPS rather than CF Workers)
uvicorn        # ASGI server for FastAPI
```
