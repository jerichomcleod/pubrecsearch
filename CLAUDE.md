# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PubRecSearch is a system for scraping, storing, and indexing U.S. federal public records to build a searchable database of individuals for investigative research purposes.

## Core Requirements (from plan.md)

1. Automated scraping of all listed data sources with deduplication tracking
2. Monitoring of scraping activity with error/gap reporting
3. Scrape metadata logging (timestamps, data fetched) for full auditability
4. File storage in Cloudflare R2
5. Individual-centric database with references to R2-stored files, data source, capture date, and relationship to source document

## Data Sources to Scrape

**Federal Financial & Tax:** IRS Form 990 (ProPublica/IRS), SEC EDGAR, USASpending.gov

**Legal & Law Enforcement:** DOJ Press Releases, PACER, OFAC SDN List, ATF FFL List, NSOPW, BOP Inmate Locator

**Elections & Political Finance:** FEC.gov, Lobbying Disclosure Act (lda.senate.gov), FARA (fara.gov)

**Professional Licensing:** FAA Airmen, FCC License Search, NPPES NPI Registry

**Healthcare:** CMS Open Payments, OIG Exclusions, FDA Debarment List

**Property, IP & Business:** USPTO Patents, USPTO Trademarks, Copyright Office, SAM.gov Exclusions

**Congressional & Executive:** Congressional Record, House/Senate/OGE Financial Disclosures

**Environment & Safety:** EPA ECHO, NTSB Accident Database, CPSC Recall Database

## Architecture Notes

This project is in the planning stage — no code exists yet. When building:

- Each data source is a separate scraper module with a consistent interface (fetch, parse, store)
- Scrape state/progress is persisted in PostgreSQL so runs are resumable without full re-scrapes
- All scraped files are uploaded to Cloudflare R2 (raw archive + periodic pg_dump backups); PostgreSQL runs locally on the host machine
- The individual entity model is central — all records link back to named persons; no entity resolution at ingest (store as-is)
- Database is **PostgreSQL** (local) — not SQLite/D1; scale analysis shows 60–120M rows at full in-scope load (see implementation_plan.md Section 2)
- Large bulk files (FEC 20 GB+ uncompressed, EDGAR index files) must be parsed with Polars lazy/streaming — never load full files into memory
- Use PostgreSQL `COPY` for bulk inserts; drop indexes before bulk load, recreate after
- Historical cutoff: **2015**. No data before 2015 is loaded. Bootstrap scripts process one year at a time.
- **Do not use LLMs for any data extraction or processing** — the data volumes make API costs prohibitive

**Out-of-scope sources (do not implement):** BOP Inmate Locator, FCC ULS, Congressional Record, FAA Airmen, NPPES NPI, USPTO Patents, USPTO Trademarks, Copyright Office, House/Senate/OGE Financial Disclosures, NTSB Accidents, CPSC Recalls, PACER, NSOPW
