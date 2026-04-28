# PubRecSearch — Project Overview

## What It Is

PubRecSearch is a batch scraping pipeline and search index for U.S. federal public records. It continuously ingests records from 14 federal government data sources, extracts named individuals from each, archives the raw source files in Cloudflare R2, and maintains a queryable PostgreSQL database linking every person to every source document in which they appear.

The system is designed for investigative research: given a name, it surfaces every federal record — sanctions list appearances, lobbying filings, campaign donations, enforcement actions, excluded contractors, and more — in a single search.

## What It Is Not

- **Not a real-time system.** Scrapers run on cron schedules; data is hours to days old depending on the source.
- **Not a deduplication or entity resolution system.** Each source record creates its own `individuals` row. The same person named "John Smith" in FEC and OFAC will appear as two separate rows. A `merge_group_id` column is reserved for future entity resolution but is not populated.
- **Not an LLM pipeline.** All extraction is deterministic (structured parsing, regex). LLMs are explicitly excluded — the data volumes make API costs prohibitive, and determinism matters for an audit trail.
- **Not a public-facing service.** The query API runs locally; there is no authentication, rate limiting, or multi-tenancy.

## Data Sources

Fourteen federal sources across four phases of implementation:

| Phase | Sources |
|-------|---------|
| 1 — Small bulk | OFAC SDN, ATF FFL, FDA Debarment, SAM.gov Exclusions, OIG LEIE |
| 2 — Large bulk | FEC Contributions, SEC EDGAR, IRS Form 990, CMS Open Payments, USASpending.gov |
| 3 — Structured APIs | LDA Lobbying Disclosure, FARA |
| 4 — HTML / enforcement APIs | DOJ Press Releases, EPA ECHO |

### Coverage at scale (2015–present)

| Table | Estimated rows at full backfill |
|-------|-------------------------------|
| `individuals` | 15–40 million |
| `individual_documents` | 60–120 million |
| `documents` | 500K–2 million |
| R2 storage | ~225–355 GB raw files |

The FEC contributions table alone accounts for 50–100 million rows per 2-year election cycle.

## System Components

```
┌─────────────────────────────────────────────────────────────────────┐
│  pubrecsearch schedule          (APScheduler, blocks indefinitely)  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  ScraperRunner (per source)                                  │   │
│  │   discover() → fetch() → upload R2 → parse() → write DB      │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
        │                              │
        ▼                              ▼
┌──────────────┐               ┌──────────────────┐
│ PostgreSQL   │               │ Cloudflare R2    │
│  (local)     │               │  (raw archive)   │
│  individuals │               │  raw/{src}/{p}/  │
│  documents   │               │  {filename}      │
│  ind_docs    │               │  pg_dump backups │
│  scrape_jobs │               └──────────────────┘
│  scrape_state│
└──────────────┘
        │
        ▼
┌─────────────────────────────────────────────┐
│  pubrecsearch serve    (FastAPI, local)     │
│  GET /search   GET /individual/{id}         │
│  GET /document/{id}   GET /dashboard        │
└─────────────────────────────────────────────┘
```

## Directory Layout

```
pubrecsearch/
├── src/pubrecsearch/           # Main Python package
│   ├── config.py               # Pydantic settings (reads .env)
│   ├── models.py               # DTOs: DownloadTarget, ParsedRecord, ParsedIndividual
│   ├── db.py                   # PostgreSQL helpers (psycopg2, COPY)
│   ├── normalize.py            # Canonical name normalization
│   ├── http_client.py          # httpx factory (OS cert store via truststore)
│   ├── r2.py                   # Cloudflare R2 / S3 uploads
│   ├── base_scraper.py         # Abstract BaseScraper interface
│   ├── runner.py               # ScraperRunner + APScheduler integration
│   ├── cli.py                  # CLI: run / schedule / serve / init-db
│   ├── monitoring.py           # structlog + Resend email alerts
│   ├── scrapers/               # 14 source-specific scrapers
│   │   ├── __init__.py         # ALL_SCRAPERS list
│   │   ├── ofac_sdn.py
│   │   ├── atf_ffl.py
│   │   ├── fda_debarment.py
│   │   ├── sam_exclusions.py
│   │   ├── oig_exclusions.py
│   │   ├── fec_contributions.py
│   │   ├── sec_edgar.py
│   │   ├── irs_990.py
│   │   ├── cms_open_payments.py
│   │   ├── usaspending.py
│   │   ├── lda.py
│   │   ├── fara.py
│   │   ├── doj_press.py
│   │   └── epa_echo.py
│   └── api/
│       └── main.py             # FastAPI app
├── scripts/bootstrap/          # One-time historical backfill scripts
│   ├── fec_bootstrap.py
│   ├── edgar_bootstrap.py
│   ├── irs990_bootstrap.py
│   ├── cms_bootstrap.py
│   ├── usaspending_bootstrap.py
│   ├── lda_bootstrap.py
│   ├── doj_bootstrap.py
│   ├── epa_bootstrap.py
│   └── sam_exclusions_import.py
├── db/
│   └── schema.sql              # PostgreSQL schema (idempotent, run once)
├── scripts/
│   └── explore_db.ipynb        # Jupyter notebook: browse and query the DB
├── docs/                       # This documentation
├── plan.md                     # Full implementation plan and design decisions
├── pyproject.toml              # Dependencies and entry points
└── .env                        # Credentials and settings (not in git)
```

## Key Design Decisions

These decisions permeate the entire codebase and explain many implementation choices:

**1. No entity resolution at ingest.** Each source record creates a new `individuals` row without attempting to match it to existing rows. The same real-world person will appear as multiple rows if they appear in multiple sources. This avoids false merges, keeps ingest fast, and defers the hard problem. A `merge_group_id` column is reserved for a future pass.

**2. PostgreSQL, local.** The database runs on the same machine as the scrapers. At 60–120M rows, SQLite is too slow and D1 lacks `COPY` support. A VPS running PostgreSQL would cost ~$24–48/month; local hardware already owned is $0.

**3. R2 as raw file archive.** Every downloaded source file is uploaded to Cloudflare R2 before any database writes happen. This provides: (a) an audit trail with the exact bytes used for extraction, (b) a way to re-run extraction without re-downloading, and (c) a source for `pg_dump` backup storage.

**4. SHA-256 deduplication at the file level.** If a scraper downloads the same file it already processed (unchanged content), the hash matches and the entire file is skipped — no re-parsing, no duplicate rows. This is the primary deduplication mechanism.

**5. Bulk COPY for large sources.** FEC, IRS 990, CMS, and USASpending use PostgreSQL's `COPY` command via a staging temp table. This is 100–1000x faster than per-row `INSERT` for millions of records.

**6. Streaming for files too large for RAM.** CMS Open Payments and similar files exceed available RAM when loaded as bytes. A `fetch_to_file()` pattern streams them to disk, hashes and uploads incrementally, and parses via Polars lazy scanning — never holding the full file in memory.

**7. Historical cutoff of 2015.** No data before January 1, 2015 is loaded. Sources with annual/quarterly file boundaries are bootstrapped year-by-year starting from 2015. This limits the initial bulk load to a manageable size while covering a decade of records.
