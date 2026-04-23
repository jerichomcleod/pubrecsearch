# PubRecSearch

A batch scraping pipeline and search index for U.S. federal public records. Continuously ingests records from federal data sources, stores raw files in Cloudflare R2, and maintains a PostgreSQL database of named individuals linked to their source documents.

## What it collects

| Source | Type | Update frequency |
|---|---|---|
| OFAC SDN List | Sanctioned individuals | Daily |
| ATF Federal Firearms Licensees | FFL sole proprietors | Monthly |
| OIG LEIE Exclusions | HHS-excluded individuals | Monthly |
| SAM.gov Exclusions | Procurement-debarred individuals | Daily |
| FDA Debarment List | FDA-debarred individuals | Weekly |

Phase 2 adds: FEC donors, SEC EDGAR insiders, IRS Form 990 officers, CMS Open Payments, USASpending recipients. Phase 3 adds: LDA lobbying, FARA foreign agents, DOJ press releases, EPA ECHO enforcement.

## Architecture

```
Scrapers (Python) → Cloudflare R2 (raw file archive)
                 → PostgreSQL (individuals + document linkages)
                 → FastAPI (search + retrieval API)
```

- Each source is a scraper module with a consistent `discover → fetch → parse` interface
- Raw source files are uploaded to R2 before any database writes
- All scrape jobs are logged to PostgreSQL for full auditability
- Name normalization is centralized so search and ingest use identical logic
- Historical cutoff: 2015. No data before 2015 is loaded.

## Requirements

- Python 3.11+
- Docker (for PostgreSQL — see [docker/README.md](docker/README.md))
- A Cloudflare R2 bucket
- A Resend account for email alerts — optional; alerts are silently skipped if not configured
- A SAM.gov API key (free — needed for SAM.gov exclusions scraper)

---

## Quickstart

### 1. Clone and install

```bash
git clone <repo-url>
cd pubrecsearch
pip install -e .
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials:

```
DATABASE_URL=postgresql://pubrecsearch:changeme@localhost:5432/pubrecsearch
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY=<key>
R2_SECRET_KEY=<secret>
R2_BUCKET=pubrecsearch-raw
SAM_API_KEY=<sam.gov api key>
EDGAR_USER_AGENT=PubRecSearch your@email.com

# Optional — leave blank to run without email alerts
RESEND_API_KEY=
ALERT_EMAIL=
```

### 3. Start PostgreSQL

```bash
docker compose -f docker/docker-compose.yml up -d
```

The schema is applied automatically on first launch. See [docker/README.md](docker/README.md) for full details.

### 4. Apply the schema (if not using Docker)

If you're connecting to an existing PostgreSQL instance instead:

```bash
pubrecsearch init-db
```

### 5. Run a scraper

```bash
# Run one source immediately
pubrecsearch run ofac_sdn

# Run all Phase 1 sources
pubrecsearch run
```

### 6. Start the scheduler

Runs all scrapers on their configured cron schedules (keeps running in the foreground):

```bash
pubrecsearch schedule
```

### 7. Start the query API

```bash
pubrecsearch serve
# → http://localhost:8000
# → http://localhost:8000/docs  (interactive API docs)
```

---

## CLI reference

```
pubrecsearch run [SOURCE_ID]   Run one scraper (or all) immediately
pubrecsearch schedule          Start the cron scheduler daemon
pubrecsearch serve             Start the FastAPI query server
pubrecsearch init-db           Apply db/schema.sql to the database
```

## API endpoints

```
GET /search?q=<name>                Search individuals by name (fuzzy)
GET /individual/<id>                Individual record + all linked documents
GET /individual/<id>/documents      Documents for an individual
GET /document/<id>                  Document metadata + presigned R2 download URL
GET /sources                        Scrape job status for all sources
GET /health                         Liveness check
```

---

## Project structure

```
pubrecsearch/
├── docker/                   PostgreSQL Docker Compose setup
├── db/
│   └── schema.sql            PostgreSQL schema (individuals, documents, jobs)
├── src/pubrecsearch/
│   ├── config.py             Settings loaded from .env
│   ├── models.py             DownloadTarget, ParsedIndividual, ParsedRecord
│   ├── normalize.py          Canonical name normalization (shared by all scrapers + API)
│   ├── db.py                 PostgreSQL helpers (jobs, state, bulk COPY)
│   ├── r2.py                 Cloudflare R2 upload / presign
│   ├── base_scraper.py       BaseScraper abstract class
│   ├── runner.py             Job orchestrator + APScheduler setup
│   ├── monitoring.py         structlog + Resend email alerts
│   ├── cli.py                CLI entry point
│   ├── scrapers/             One module per data source
│   └── api/main.py           FastAPI query API
└── tests/
```
