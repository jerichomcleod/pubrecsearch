# PubRecSearch — Scraping Implementation Plan

This document covers the 14 in-scope data sources. Each entry documents permissibility, access method, rate limits, incremental strategy, file format, parse targets, and known gotchas. Permissibility was verified against robots.txt and published terms of service where available.

---

## Sources Overview

| # | Source | Access Method | Auth Required | Rate Limit | Permissible | Update Freq |
|---|---|---|---|---|---|---|
| 1 | FEC Contributions | S3 bulk ZIP download | None | None (S3) | ✅ Explicit | Daily / cycle |
| 2 | SEC EDGAR | REST API + nightly bulk ZIP | None (User-Agent req'd) | 10 req/sec | ✅ Explicit | Nightly ~3am ET |
| 3 | USASpending | Bulk download API | None | Undocumented | ✅ Official API | Ongoing |
| 4 | IRS Form 990 | Monthly ZIP (XML) via IRS TEOS | None | Undocumented | ✅ Explicit | Monthly |
| 5 | DOJ Press Releases | RSS feeds + HTML | None | Undocumented | ✅ RSS documented | Continuous |
| 6 | OFAC SDN List | Direct XML/ZIP download | None | None documented | ✅ Explicit | Daily |
| 7 | ATF FFL | Monthly text file download | None (verify) | None documented | ⚠️ Verify download URL | Monthly |
| 8 | LDA Lobbying | REST API v1 | Free API key | 120 req/min | ✅ Official ToS | Quarterly |
| 9 | FARA | Bulk CSV/XML ZIP | None | None documented | ✅ Bulk portal | Daily |
| 10 | CMS Open Payments | Annual bulk CSV ZIP | None | None documented | ✅ Explicit | Annually (June) |
| 11 | OIG Exclusions (LEIE) | Single CSV (fixed URL) | None | None documented | ✅ Explicit | Monthly |
| 12 | FDA Debarment | Excel download (XLSX) | None | None documented | ✅ Explicit | Ad-hoc |
| 13 | SAM.gov Exclusions | REST API v4 | Free API key | 1,000 req/day (free key) | ✅ Official API | Daily |
| 14 | EPA ECHO | REST API | None | Undocumented | ✅ Official API | Varies |

---

## 1. FEC Individual Contributions

### Permissibility
**Explicitly permitted.** robots.txt at `https://www.fec.gov/robots.txt` blocks interactive query endpoints (`/data/candidates/?*`, `/data/filings/?*`, etc.) with a 10-second crawl delay, but bulk S3 downloads are entirely separate infrastructure and are the FEC's intended mechanism for automated bulk access.

### Access Method
Bulk ZIP files hosted on an AWS GovCloud S3 bucket — not the fec.gov website itself. No scraping involved; direct S3 file downloads.

**Canonical URLs:**
```
# Individual contributions by cycle year (2-year cycle files)
https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com/bulk-downloads/[YEAR]/indiv[YY].zip

# Current cycle index
https://www.fec.gov/data/browse-data/?tab=bulk-data

# OpenFEC API (supplementary — use for incremental updates after initial bulk load)
https://api.open.fec.gov/v1/schedules/schedule_a/
```

### Authentication
None for bulk S3 downloads. Optional free API key for OpenFEC API (`https://api.open.fec.gov/developers/`) raises rate limit from 20 to 1,000 req/hour.

### Rate Limits
- S3 bulk downloads: none documented; standard S3 throughput
- OpenFEC API: 20 req/hour unauthenticated; 1,000 req/hour with free key

### Incremental Strategy
- **Initial load (2015–present):** Download `indiv15.zip` through `indiv24.zip` — one ZIP per 2-year cycle. Each ZIP contains a single large pipe-delimited CSV.
- **Ongoing:** OpenFEC API supports `last_index` cursor pagination on `/schedules/schedule_a/` for contributions received since a given date. Poll monthly for new transactions in the current cycle. Download full ZIP at cycle close.
- **Dedup:** Store last-seen transaction ID from API; for bulk files, store SHA-256 of the ZIP to detect updates.

### File Format
- Format: ZIP containing a single pipe-delimited (`|`) CSV file
- Encoding: UTF-8
- Size (recent cycle): ~3–5 GB compressed per 2-year cycle; ~15–20 GB uncompressed
- Key fields for extraction: `NAME` (contributor), `CITY`, `STATE`, `ZIP_CODE`, `EMPLOYER`, `OCCUPATION`, `TRANSACTION_DT`, `TRANSACTION_AMT`, `CMTE_ID`, `AMNDT_IND`
- Individual names appear in `NAME` as `LAST, FIRST MIDDLE` format

### Parse Targets
- Relationship: `fec_donor`
- Source identifier: `SUB_ID` (unique per transaction)
- Individual name from `NAME` field
- Additional context: employer, occupation, amount, committee, date stored as JSON in `excerpt`/`identifiers`

### Gotchas
- Pipe-delimited, not comma-delimited; specify `sep='|'` in Polars
- No header row in the contribution file; column order defined in the [FEC data dictionary](https://www.fec.gov/campaign-finance-data/contributions-individuals-file-description/)
- Names include business names (PAC intermediaries) mixed with individual names; filter by `ENTITY_TP = 'IND'`
- Very large files; use Polars `scan_csv()` (lazy) with streaming; never `read_csv()`
- Cycle files use 2-year cycle naming: `indiv20.zip` covers 2019–2020

---

## 2. SEC EDGAR

### Permissibility
**Explicitly permitted and designed for programmatic access.** SEC publishes developer documentation at `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`. A `User-Agent` header identifying the accessing organization/individual and an email address is required in all requests — failure to include it will result in blocks. robots.txt could not be fetched (403), but SEC's own documentation is the authoritative source for access policy.

### Access Method
Two complementary interfaces:
1. **Nightly bulk ZIP files** — entire submission history and company facts, updated ~3am ET daily. Best for initial load.
2. **REST API at `data.sec.gov`** — JSON, for incremental access after initial load.
3. **Quarterly full-index files** — index of all filings per quarter, good for scoping to specific form types.

**Canonical URLs:**
```
# Nightly bulk archives (updated ~3am ET)
https://data.sec.gov/submissions.zip       # all company submission histories
https://data.sec.gov/companyfacts.zip      # all XBRL financial data (not needed)

# Quarterly full-index (lists all filings; filter for form types in-scope)
https://www.sec.gov/Archives/edgar/full-index/[YEAR]/QTR[1-4]/company.idx
https://www.sec.gov/Archives/edgar/full-index/[YEAR]/QTR[1-4]/form.idx

# Daily index (incremental, after initial load)
https://www.sec.gov/Archives/edgar/daily-index/[YEAR]/QTR[1-4]/form[MMDDYYYY].idx

# Individual company submissions via API (JSON)
https://data.sec.gov/submissions/CIK{10-digit-cik}.json

# Individual filing document
https://www.sec.gov/Archives/edgar/data/{CIK}/{accession-no}/{filename}
```

### Authentication
None. **Required `User-Agent` header format:**
```
User-Agent: PubRecSearch admin@youremail.com
```
Failure to include this header is the most common reason for EDGAR blocks.

### Rate Limits
**10 requests/second.** This is a hard limit; exceeding it results in temporary blocks. Implement a rate limiter (token bucket or `asyncio.Semaphore`) enforcing ≤10 req/sec.

### Incremental Strategy
- **Initial load (2015–present):** Download quarterly `form.idx` files for 2015Q1 through current quarter. Filter for in-scope form types: `4` (insider transactions), `3` (initial ownership), `SC 13G`, `SC 13G/A`, `SC 13D`, `SC 13D/A`. For each matching filing, download the filing document from the Archives.
- **Ongoing:** Poll `daily-index/` for current quarter's daily form index; process new entries matching in-scope form types.
- **Dedup:** Store accession number in `scrape_state.cursor` as a set of processed accession numbers (or max accession date).

### File Format
- Quarterly index: fixed-width text (`.idx`) with columns: Company Name, Form Type, CIK, Date Filed, Filename
- Filing documents: XML (Form 4/3), HTML/SGML (older filings)
- Size: Quarterly `form.idx` ~5–10 MB; individual Form 4 XML ~5–15 KB each

### Parse Targets
- Relationship types: `sec_insider` (Form 4/3), `sec_large_shareholder` (SC 13G/D)
- From Form 4 XML: `<reportingOwner>` → name, CIK; `<issuer>` → company; transaction details
- From SC 13G/D: filer name from cover page (XML header)
- Source identifier: EDGAR accession number (`{CIK}-{YY}-{NNNNNN}`)

### Gotchas
- `submissions.zip` is large (~2 GB); use only for initial CIK lookup if needed; prefer index files for form-type filtering
- Form 4 XML schema changed over time; handle both pre-2004 SGML and modern XML variants
- Some filings are HTML or text, not XML; detect by file extension in the index
- CIK numbers are zero-padded to 10 digits in API paths but not always in index files
- Rate limit is per IP; if running concurrent requests, the limit applies to all combined

---

## 3. USASpending.gov

### Permissibility
**Permitted via official public API.** robots.txt blocks URLs with query parameters (`/*?*`) and `.php` paths, but the REST API at `api.usaspending.gov` is the officially documented public interface and is not covered by that restriction. No ToS or data restriction found.

### Access Method
REST API with bulk CSV download capability. Two relevant endpoints:
1. **Award Data Archive** — pre-generated annual CSV files per award type; best for initial load
2. **Bulk Download API** — on-demand filtered CSV generation

**Canonical URLs:**
```
# Award Data Archive (pre-generated annual files — best for bulk)
https://www.usaspending.gov/download_center/award_data_archive

# Archive direct file pattern (verify current filenames on archive page)
# Contracts: FY[YEAR]_All_Contracts_Full_20YYMMDD.zip
# Assistance: FY[YEAR]_All_Assistance_Full_20YYMMDD.zip

# Bulk Download API (on-demand, for filtered subsets)
https://api.usaspending.gov/api/v2/bulk_download/awards/

# API documentation
https://api.usaspending.gov/
```

### Authentication
None required. No API key.

### Rate Limits
Not documented. Treat conservatively: max 5 concurrent requests; 1-second delay between requests.

### Incremental Strategy
- **Initial load:** Download annual award archive ZIPs for FY2015–FY2024. Filter individual recipients at parse time (see Parse Targets).
- **Ongoing:** Quarterly updated archive files are published for the current fiscal year. Poll archive page monthly; compare file modification date or hash against stored cursor.
- **Dedup:** Store filename + SHA-256 of each downloaded archive in `scrape_state`.

### File Format
- Format: ZIP containing multiple CSV files (split by size threshold)
- Encoding: UTF-8
- Size: FY annual contracts file ~2–8 GB compressed; assistance ~500 MB–2 GB
- Key fields for individual extraction: `recipient_name`, `recipient_uei`, `business_types_codes`, `recipient_address_line_1`, `recipient_city_name`, `recipient_state_code`

### Parse Targets
- Relationship: `usaspending_recipient`
- Filter rows where `business_types_codes` contains `'I'` (Individual) or sole proprietor indicators (`'2X'`, `'23'`)
- Individual name from `recipient_name`
- Source identifier: `award_id_fain` or `award_id_piid`

### Gotchas
- Archive page lists the most recent full-year files; older years may be at different paths — verify URLs on the archive page before scraping
- `business_types_codes` is a multi-value field (comma-separated codes); filter with `str.contains('I')`
- Individual-recipient records are a small fraction (~1–5%) of total awards; most are companies
- USASpending derives its data from agency reporting and can contain duplicates; dedup on `award_id`

---

## 4. IRS Form 990

### Permissibility
**Explicitly permitted.** IRS publishes 990 EFILE data for public download. Note: the AWS S3 bucket (`s3://irs-form-990/`) was **deprecated December 31, 2021** and is no longer updated. Use IRS TEOS (Tax Exempt Organization Search) instead.

### Access Method
Monthly ZIP files containing XML 990 filings, published at the IRS TEOS download page. Also available: annual CSV index files mapping EINs to filing metadata.

**Canonical URLs:**
```
# TEOS download page (verify current file listing here)
https://www.irs.gov/charities-non-profits/form-990-series-downloads

# Monthly XML ZIPs (filename pattern — verify on download page)
https://apps.irs.gov/pub/epostcard/990/xml/[YEAR]/[YEAR]_TEOS_XML_[MM]A.zip
# Example: 2024_TEOS_XML_01A.zip through 2024_TEOS_XML_12A.zip

# Annual CSV index
https://apps.irs.gov/pub/epostcard/990/xml/[YEAR]/index_[YEAR].csv

# ProPublica Nonprofit Explorer API (alternative; includes pre-2015 data)
https://projects.propublica.org/nonprofits/api/v2/organizations/{ein}.json
https://projects.propublica.org/nonprofits/api/v2/organizations.json?q={name}
```

### Authentication
None for IRS downloads. ProPublica API: none; no key required.

### Rate Limits
- IRS: Not documented; treat as standard web server (max 2 req/sec, single-threaded)
- ProPublica: Not published; be conservative (1 req/sec)

### Incremental Strategy
- **Initial load:** Download `index_{YEAR}.csv` for 2015–current to get filing metadata. Download monthly XML ZIPs for each year 2015–present. Extract XML per filing from ZIP.
- **Ongoing:** New monthly ZIPs are published throughout the year. Poll the download page monthly; compare against stored list of downloaded files.
- **Dedup:** Store `{YEAR}_TEOS_XML_{MM}A.zip` filenames as processed in `scrape_state`. Individual filing dedup by `ReturnId` or `EIN + TaxPeriod`.

### File Format
- Format: ZIP containing individual XML files, one per 990 filing
- Encoding: UTF-8 (XML)
- Size: Each monthly ZIP ~50–300 MB; each individual XML ~20–500 KB
- Key fields: `ReturnHeader/Filer/EIN`, `ReturnHeader/TaxPeriodEndDt`, `ReturnData/IRS990/...`

### Parse Targets
- Relationship types: `nonprofit_officer`, `nonprofit_director`, `nonprofit_contractor`, `nonprofit_hce` (highly compensated employee)
- Source: `IRS990/Form990PartVIISectionA` — officer/director compensation table
- Also: `IRS990ScheduleL` for loans to/from officers; `IRS990ScheduleO` for supplemental
- Name from `PersonNm` or `BusinessName/BusinessNameLine1Txt`; role from `TitleTxt`
- Source identifier: `ReturnId` (IRS-assigned) or `EIN + TaxPeriodEndDt`

### Gotchas
- 990 XML schema has many versions across years; use `xsd:type` attribute to determine which schema applies. The namespace/element paths differ between 990, 990-EZ, and 990-PF
- Some filers (churches, small orgs) file 990-N (e-postcard) — these are not in the XML ZIPs; they contain no individual data anyway
- `YEAR` in filename is the processing year, not the tax year; a 2024 file may contain 2023 tax year returns
- ProPublica API is useful for targeted EIN lookups but not for bulk extraction; prefer IRS bulk for completeness
- Old S3 bucket (`irs-form-990`) is dead as of Dec 31, 2021; ignore any references to it

---

## 5. DOJ Press Releases

### Permissibility
**Permitted via RSS and documented API.** DOJ publishes RSS feeds and a developer API at `https://www.justice.gov/developer`. RSS feeds are the recommended incremental interface. Standard HTML scraping of public news pages is not prohibited. No robots.txt fetch succeeded (403), but DOJ explicitly provides automated access endpoints, indicating programmatic access is intended.

### Access Method
Three usable interfaces:
1. **RSS feeds** — per-component press release feeds; simplest for incremental access
2. **DOJ News API** — JSON, documented at `https://www.justice.gov/developer`
3. **HTML scraping** — fallback for full text and older content not in RSS

**Canonical URLs:**
```
# All DOJ press releases RSS
https://www.justice.gov/news/press-releases/rss.xml

# Component-specific RSS feeds (subset — use these for targeted feeds)
https://www.justice.gov/usao/rss          # US Attorneys Office
https://www.justice.gov/atr/rss           # Antitrust Division
https://www.justice.gov/criminal/rss      # Criminal Division
https://www.justice.gov/opa/rss           # Office of Public Affairs

# DOJ Developer API (JSON)
https://www.justice.gov/api/resources/press-releases.json

# Web pages for HTML fallback
https://www.justice.gov/news/press-releases?page={N}
```

### Authentication
None required.

### Rate Limits
Not documented. RSS and API are designed for public consumption; max 1 req/sec for HTML pages, burst-free for RSS.

### Incremental Strategy
- **Initial load (2015–present):** Paginate the HTML press release listing (`?page=N`) backwards from current date to 2015-01-01. Each page lists ~25 releases with title, date, URL. Fetch each release page for full text.
- **Ongoing:** Poll the main RSS feed daily. Parse new items since last `pubDate` stored in `scrape_state.cursor`.
- **Dedup:** Store last-seen RSS `guid` (DOJ URL) in cursor. On first run, bulk-paginate to 2015.

### File Format
- RSS items: title, link, pubDate, description (partial text)
- Full press release: HTML page; full text in `<div class="field-items">`
- Encoding: UTF-8

### Parse Targets
- Relationship: `doj_defendant`, `doj_subject`
- Named defendant extraction strategy (deterministic, no LLM):
  1. Title regex patterns: `"United States v. [Name]"`, `"[Name] Sentenced"`, `"[Name] Pleads Guilty"`, `"[Name] Charged"`, `"[Name] Convicted"`, `"[Name] Indicted"`
  2. Body regex patterns: look for `defendant [First Last]`, `[First Last], of [City]`, `[First Last], age [NN]`
  3. Store the full title + first 500 chars of body as `excerpt`; flag confidence level (`high` for "v." pattern, `medium` for body regex)
- Source identifier: DOJ press release URL (slug is stable)

### Gotchas
- Named entity extraction from free text will produce false positives and false negatives; this is expected and acceptable — store confidence level and raw excerpt
- DOJ has ~50+ component offices; the main RSS feed aggregates all of them but may miss some older component feeds
- Some press releases name organizations rather than individuals; filter to releases with patterns matching human names (presence of comma-separated last/first or `of [City], [State]` pattern)
- Do not attempt to scrape the PACER case links sometimes embedded in DOJ press releases; those require authentication

---

## 6. OFAC SDN List

### Permissibility
**Explicitly permitted and encouraged.** OFAC's Sanctions List Service documentation explicitly states that firms should set up scheduled automated downloads. HTTPS is required. No authentication, no rate limits documented.

### Access Method
Direct file download. The full SDN list and consolidated list are updated daily and published at fixed URLs.

**Canonical URLs:**
```
# Primary SDN XML (full Specially Designated Nationals list)
https://www.treasury.gov/ofac/downloads/sdn.xml
https://www.treasury.gov/ofac/downloads/sdnlist.pdf  # PDF reference (not for parsing)

# SDN in delimited format
https://www.treasury.gov/ofac/downloads/sdn.csv
https://www.treasury.gov/ofac/downloads/alt.csv      # aliases
https://www.treasury.gov/ofac/downloads/add.csv      # addresses

# Consolidated Sanctions List (SDN + other lists)
https://www.treasury.gov/ofac/downloads/consolidated/consolidated.xml
https://www.treasury.gov/ofac/downloads/consolidated/consolidated.csv

# Advanced Sanctions Data Standard (OFAC's newer XML format)
https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML
```

### Authentication
None. HTTPS required.

### Rate Limits
None documented. One download per scheduled run is the intended pattern; do not poll continuously.

### Incremental Strategy
- **Full replacement:** The SDN list is not append-only; entries are added, modified, and removed. There is no change log or incremental format. Download the full XML on each run.
- **Dedup:** Store SHA-256 hash of the downloaded XML file in `scrape_state.cursor`. Only re-parse if hash changed (typically changes daily).
- **Schedule:** Daily at off-peak hours.

### File Format
- Primary format: XML (`sdn.xml`) — structured, most complete
- Also: CSV (three separate files for main data, aliases, addresses) — simpler to parse but split across files
- Encoding: UTF-8 (XML), UTF-8 (CSV)
- Size: `sdn.xml` ~25–30 MB; `consolidated.xml` ~60 MB

### Parse Targets
- Relationship: `ofac_sanctioned`
- Filter `<sdnEntry>` where `<sdnType>` = `"Individual"`
- Name from `<lastName>`, `<firstName>` or `<sdnName>` for aliases
- Additional: nationality, date of birth, place of birth, ID documents — store as JSON in `identifiers`
- Source identifier: OFAC `<uid>` (integer, stable per entry)

### Gotchas
- `sdn.xml` and `consolidated.xml` use slightly different schemas; pick one and be consistent. `sdn.xml` is simpler for individuals.
- Aliases in `<akaList>` are separate name entries; create alias records in the `individuals.aliases` JSON field
- Entity entries (`<sdnType>` = `"Entity"`, `"Vessel"`, `"Aircraft"`) should be filtered out
- The ADVANCED XML format (`SDN_ADVANCED.XML`) is a newer schema with richer data; worth using but schema is more complex — verify against OFAC documentation before committing to it

---

## 7. ATF Federal Firearms Licensee List

### Permissibility
**Likely permitted; verify the specific download mechanism.** ATF publishes FFL data as a public record. The official download appears to be via the FFL eZ Check system at `https://fflezcheck.atf.gov/`. The direct download mechanism requires verification — it may be a form-based download without authentication, or it may require account creation. The data itself is explicitly described as public.

### Access Method
**Requires verification before implementation.** Known options:
1. **ATF website** — `https://www.atf.gov/firearms/listing-federal-firearms-licensees` lists monthly download files; these appear to be public text files
2. **FFL eZ Check** — `https://fflezcheck.atf.gov/FFLEzCheck/fflDownloadDisplay.action` — may require login (verify)
3. **Gun Violence Archive / third-party** — maintains processed CSV copies but introduces a dependency on a third party

**Canonical URL to verify:**
```
# ATF listing page (check for monthly download links)
https://www.atf.gov/firearms/listing-federal-firearms-licensees

# eZ Check download (check if login required)
https://fflezcheck.atf.gov/FFLEzCheck/fflDownloadDisplay.action
```

**Action required before implementation:** Make one GET request to `https://www.atf.gov/firearms/listing-federal-firearms-licensees` to check if monthly files are directly linked. Check `https://www.atf.gov/robots.txt`.

### Authentication
Unknown; may be none for the ATF listing page links.

### Rate Limits
Not documented. Monthly download; single file retrieval.

### Incremental Strategy
- **Monthly replacement:** Download new monthly file when published. Store filename or file hash as cursor.
- ATF FFL data is a current-state snapshot; entries appear, renew, and expire. Not append-only.

### File Format
- Format: Pipe-delimited text file (`.txt`)
- Encoding: Likely Latin-1 / Windows-1252 (government legacy systems)
- Size: ~10–15 MB per monthly file
- Key fields: `license_regn`, `license_dist`, `license_cnty`, `license_type`, `expir_date`, `lic_seqn`, `lic_name`, `lic_bus_name`, `premise_street`, `premise_city`, `premise_state`, `premise_zip_code`, `mail_street`, `mail_city`, `mail_state`, `mail_zip_code`, `voice_phone`

### Parse Targets
- Relationship: `atf_ffl_licensee`
- Individual identification: sole proprietors where `lic_name` (licensee personal name) differs from `lic_bus_name` (business name), or where `lic_bus_name` matches individual name patterns
- License type filter: Type 01 (Dealer in Firearms), Type 03 (Collector of Curios), Type 06 (Manufacturer of Ammunition), Type 07 (Manufacturer of Firearms) are most relevant for individual dealers
- Source identifier: Composite of `license_regn + license_dist + license_type + license_cnty + lic_seqn`

### Gotchas
- `lic_name` is the licensee (may be individual name or officer); `lic_bus_name` is the business name — individuals operating as sole proprietors often have personal names in both fields
- Encoding: try UTF-8 first; fall back to `latin-1` on decode error
- File does not include a header row; column order must be sourced from ATF documentation
- Collectors (license type 03) and some manufacturers are excluded from the public download per ATF policy

---

## 8. LDA Lobbying Disclosure

### Permissibility
**Explicitly permitted with documented ToS.** The LDA Senate API has official terms of service at `https://lda.senate.gov/api/tos/`. Key obligations: must register for free API key; must include retrieval date and disclaimer when citing data; must not claim data endorses a product/view; Senate reserves right to block users who exceed rate limits or attempt circumvention.

### Access Method
REST API v1. JSON responses. Free API key registration required before first use.

**Registration:** `https://lda.senate.gov/api/register/`

**Canonical URLs:**
```
# API base
https://lda.senate.gov/api/v1/

# Filings (LD-2 quarterly activity reports)
https://lda.senate.gov/api/v1/filings/?filing_year={YYYY}&filing_period={period}&page={N}

# Registrations (LD-1)
https://lda.senate.gov/api/v1/registrations/?page={N}

# Contributions (LD-203)
https://lda.senate.gov/api/v1/contributions/?filing_year={YYYY}&page={N}

# API documentation (ReDoc)
https://lda.senate.gov/api/redoc/v1/
```

### Authentication
**Required.** Free API key obtained by registering at `https://lda.senate.gov/api/register/`. Pass as `Authorization: Token {api_key}` header.

### Rate Limits
- **With API key:** 120 requests/minute
- **Without API key:** 15 requests/minute (do not use unauthenticated)
- Senate reserves right to block users attempting to circumvent limits

### Incremental Strategy
- **Initial load:** Paginate all filings from `filing_year=2015` forward by year and filing period (Q1–Q4). Store `filing_uuid` for each processed filing.
- **Ongoing:** Query `filing_year={current_year}` and `filing_period={current_quarter}`. New filings have been submitted since last run. Filter by `received_dt` >= last run timestamp stored in cursor.
- **Pagination:** API uses page-based pagination; `next` URL provided in response. Max page size: check API docs (typically 25–100).

### File Format
- JSON responses
- Key filing fields: `registrant` (object with `name`, `id`), `lobbyists` (array with `lobbyist_first_name`, `lobbyist_last_name`, `lobbyist_suffix`), `lobbying_activities` (array with `general_issue_code`, `description`)

### Parse Targets
- Relationship: `lda_lobbyist`
- Individual from `lobbyists[].lobbyist_first_name + lobbyist_last_name`
- Also: `registrant` contact individual from `registrant.contact_name` (separate person)
- Source identifier: `filing_uuid` (UUID per filing)

### Gotchas
- `lobbyists` array can be empty for LD-1 registrations (no active lobbying listed yet); LD-2 filings have the actual lobbyist lists
- Amendments (`filing_type = "AMENDMENT"`) update earlier filings; link via `registrant_id + filing_period + filing_year` and use the amendment as the canonical record
- The `income` and `expenses` fields contain dollar amounts as strings (may include `"$1,000,000"` format); strip commas and `$` before storing
- LDA data starts from 1999; querying from 2015 requires `filing_year__gte=2015` filter parameter (verify exact param name in API docs)

---

## 9. FARA (Foreign Agents Registration Act)

### Permissibility
**Explicitly permitted.** FARA provides a dedicated bulk data portal at `https://efile.fara.gov/ords/fara/f?p=API:BULKDATA`. DOJ also maintains a CSV import page. No authentication required for bulk downloads.

### Access Method
Bulk ZIP downloads containing CSV or XML files. Multiple dataset types available; updated daily.

**Canonical URLs:**
```
# FARA Bulk Data portal
https://efile.fara.gov/ords/fara/f?p=API:BULKDATA

# Direct download URLs (verify current filenames on bulk data page)
https://efile.fara.gov/bulk/zip/FARA_All_Registrants.csv.zip
https://efile.fara.gov/bulk/zip/FARA_All_Registrants.xml.zip
https://efile.fara.gov/bulk/zip/FARA_All_Documents.csv.zip
https://efile.fara.gov/bulk/zip/FARA_All_Supplemental_Statements.csv.zip
https://efile.fara.gov/bulk/zip/FARA_All_Short_Form.csv.zip  # Short-form registrations

# DOJ CSV import alternative
https://www.justice.gov/nsd-fara/efile-import-csv-data
```

### Authentication
None required.

### Rate Limits
Not documented. Daily download of full files is the intended pattern.

### Incremental Strategy
- **Full replacement:** Like OFAC, FARA bulk files are full snapshots (not append-only). Download full file on each run.
- **Dedup:** SHA-256 hash of downloaded ZIP stored as cursor. Re-parse only on hash change.
- **Schedule:** Daily.

### File Format
- Format: ZIP containing CSV files
- **Encoding: ISO-8859-1 (Latin-1 / ANSI)** — this is explicitly noted in FARA documentation; use `encoding='iso-8859-1'` when reading
- Size: Full registrants CSV ZIP ~5–20 MB
- Key fields in `FARA_All_Registrants.csv`: `RegistrantID`, `Name`, `Address`, `RegistrationDate`, `TerminationDate`, `RegistrantType`

### Parse Targets
- Relationship: `fara_registrant`
- Filter `RegistrantType` = `"Individual"` (as opposed to `"Organization"`)
- For organizations: extract individual contacts from `Short_Form` data (Form NSD-6 short-form registrations) which list individual lobbyists
- Source identifier: `RegistrantID` (FARA-assigned integer)

### Gotchas
- ISO-8859-1 encoding; parse will fail silently or corrupt names if opened as UTF-8 — always specify encoding explicitly
- `TerminationDate` may be empty (still active) or populated (terminated); both are valid records to keep
- The bulk data page lists many file types; prioritize `FARA_All_Registrants` and `FARA_All_Short_Form` for individual extraction
- FARA active registrant count (~500) is small; total file including historical terminated registrants is larger

---

## 10. CMS Open Payments

### Permissibility
**Explicitly permitted.** CMS publishes Open Payments data for public download as part of the Physician Payments Sunshine Act transparency mandate. No ToS restrictions found on programmatic access.

### Access Method
Annual bulk CSV downloads per program year (PY). Published on or before June 30 each year for the prior program year.

**Canonical URLs:**
```
# Open Payments data download page
https://openpaymentsdata.cms.gov/

# Dataset download (navigate to "Download Data" section)
# Verify current URLs — CMS updates these annually

# CMS data.gov alternative (more stable URL patterns)
https://data.cms.gov/provider-data/

# API (for targeted queries, not bulk)
https://openpaymentsdata.cms.gov/api/1/datastore/query
```

**Action required:** Visit `https://openpaymentsdata.cms.gov/` to retrieve current download URLs for PY2015–PY2024 bulk ZIPs. CMS does not guarantee stable bulk file URLs year-over-year.

### Authentication
None required.

### Rate Limits
Not documented. Annual bulk download; not a high-frequency access pattern.

### Incremental Strategy
- **Initial load:** Download all available program year ZIPs (PY2015 is first full year within our cutoff; earlier data available but out of scope).
- **Ongoing:** New program year published annually by June 30. Poll the download page each July; download new year when available.
- **Dedup:** Track downloaded program years in `scrape_state`. Each PY is a complete snapshot for that year.
- Note: CMS refreshes prior-year data in January with late submissions; check if already-downloaded years have been updated (compare against stored file hash).

### File Format
- Format: ZIP containing multiple CSV files (split by category: General Payments, Research Payments, Ownership & Investment Interest)
- **Delimiter: Pipe (`|`) — NOT comma**
- Encoding: UTF-8
- Size: PY2024 ~3–5 GB compressed; exceeds Excel row limits (use Polars streaming)
- Key fields: `Covered_Recipient_First_Name`, `Covered_Recipient_Last_Name`, `Covered_Recipient_Type`, `Covered_Recipient_NPI`, `Covered_Recipient_Specialty_1`, `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name`, `Total_Amount_of_Payment_USDollars`, `Date_of_Payment`, `Nature_of_Payment_or_Transfer_of_Value`

### Parse Targets
- Relationship: `cms_payment_recipient`, `cms_ownership_holder`
- Filter `Covered_Recipient_Type` = `"Covered Recipient Physician"` or `"Covered Recipient Non-Physician Practitioner"` (excludes teaching hospitals)
- Individual name: `Covered_Recipient_First_Name` + `Covered_Recipient_Last_Name`
- Source identifier: CMS-assigned `Record_ID` (unique per payment record) or `Covered_Recipient_NPI` (cross-reference identifier)

### Gotchas
- Pipe-delimited: specify `sep='|'` in Polars
- Each program year ZIP contains multiple CSV files (3 payment types + metadata); process all three and link to the same individual
- `Covered_Recipient_NPI` is present for most records and provides a strong cross-source identifier (NPI links to NPPES, if that source is ever added)
- Some records have no NPI (pre-2017 data quality); fall back to name-based linkage for those

---

## 11. OIG Exclusions (LEIE)

### Permissibility
**Explicitly permitted.** OIG provides CSV downloads with right-click instructions implying direct/automated download is the intended access method. robots.txt blocks PDF report paths but not the LEIE CSV download.

### Access Method
Single CSV file at a fixed URL. The file is updated monthly and replaced in place (filename remains constant, contents change).

**Canonical URLs:**
```
# Current exclusions list (full replacement monthly)
https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv

# Download page
https://oig.hhs.gov/exclusions/exclusions_list.asp

# OIG LEIE API (supplementary — allows date-filtered queries)
https://oig.hhs.gov/exclusions/docs/LEIE_API_USER_GUIDE.pdf
# API base: https://api.exclusions.oig.hhs.gov/
```

### Authentication
None required.

### Rate Limits
Not documented. Single file download; not a high-frequency pattern.

### Incremental Strategy
- **Full replacement:** Download `UPDATED.csv` monthly. Compare SHA-256 against stored hash; re-parse only on change.
- **Dedup:** The `EXCL_DATE` and `LASTNAME + FIRSTNAME + MIDNAME` combination identifies unique individuals. Store hash of last-downloaded file.

### File Format
- Format: CSV (comma-delimited)
- Encoding: UTF-8
- Size: ~5–10 MB (~79,000 records)
- Key fields: `LASTNAME`, `FIRSTNAME`, `MIDNAME`, `BUSNAME` (business name if entity), `GENERAL`, `SPECIALTY`, `UPIN`, `NPI`, `DOB`, `ADDRESS`, `CITY`, `STATE`, `ZIP`, `EXCL_TYPE`, `EXCL_DATE`, `REINSTATE_DATE`, `WAIVERSTATE`

### Parse Targets
- Relationship: `oig_excluded`
- Filter rows where `BUSNAME` is empty (individual) or where `LASTNAME` is a person surname (heuristic: no LLC/Inc/Corp in name)
- Individual name: `FIRSTNAME + MIDNAME + LASTNAME`
- Source identifier: `NPI` if present; otherwise `UPIN`; fallback to composite name + `EXCL_DATE`

### Gotchas
- `BUSNAME` is populated for organization exclusions; for individuals it is typically empty — filter accordingly
- `REINSTATE_DATE` is populated if the exclusion was lifted; retain these records (they are historically significant)
- `NPI` field is present for healthcare providers (strong cross-source key); many older records predate NPI system and will have empty NPI

---

## 12. FDA Debarment List

### Permissibility
**Permitted.** FDA publishes the debarment list publicly. No robots.txt restriction found (404 on robots.txt request). OpenFDA provides a documented JSON API as an alternative to the Excel file.

### Access Method
**Official source:** Excel files (XLSX) per debarment category, linked from the FDA debarment page.
**Preferred alternative:** OpenFDA JSON API — more automation-friendly than Excel.

**Canonical URLs:**
```
# FDA debarment page (contains links to XLSX downloads)
https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/fda-debarment-list-drug-product-applications

# OpenFDA debarment endpoint (alternative; JSON)
https://api.fda.gov/drug/enforcement.json  # Note: enforcement actions, not debarment specifically
# Check openFDA documentation for dedicated debarment endpoint
https://open.fda.gov/apis/

# Direct XLSX download example (verify current filenames from the page)
# Filename pattern varies; inspect the debarment page HTML to find current links
```

**Action required:** Fetch `https://www.fda.gov/.../fda-debarment-list-drug-product-applications` once to extract current XLSX download URLs. Check `https://www.fda.gov/robots.txt` before making additional requests.

### Authentication
None required. OpenFDA API: no key required for <240 req/min; free API key available for higher limits.

### Rate Limits
- OpenFDA: 240 requests/minute without key; 120,000 requests/day with free key
- Excel download: No limit documented; single-file download

### Incremental Strategy
- **Full replacement:** Debarment list is small (~100–200 entries). Download and re-parse the full file on each run.
- **Dedup:** Store SHA-256 hash of downloaded file; re-parse only on change.
- **Schedule:** Monthly (changes infrequently).

### File Format
- **Official:** Excel XLSX (multiple sheets, one per debarment category)
- **Alternative:** OpenFDA JSON
- Size: Very small (<1 MB)
- Key fields: Name, Debarment Type, Effective Date, Debarment Period

### Parse Targets
- Relationship: `fda_debarred`
- All entries on the individual debarment list are persons (company debarments are on a separate list)
- Source identifier: Name + Effective Date (no stable numeric ID)

### Gotchas
- FDA provides multiple separate debarment lists (drug product applications, food import, drug import, tobacco); need to download and parse all relevant sheets
- XLSX format requires `openpyxl` library; add to dependencies
- OpenFDA may not have all debarment categories; verify coverage against the HTML page before relying on it exclusively
- The debarment page HTML must be parsed to find current download links (URLs include dates or version numbers that change)

---

## 13. SAM.gov Exclusions

### Permissibility
**Explicitly permitted via official API.** SAM.gov provides a documented API with clear terms. robots.txt blocks search and admin paths but not API endpoints. A free API key is required; register at `https://sam.gov/profile/details`.

### Access Method
REST API v4 (JSON). V1–V3 were retired September 2024. CSV extraction endpoint available for large bulk queries with async delivery.

**Canonical URLs:**
```
# Production API base
https://api.sam.gov/entity-information/v4/exclusions

# API documentation
https://open.gsa.gov/api/exclusions-api/

# API key registration
https://sam.gov/profile/details

# Example query: individual exclusions updated since a date
https://api.sam.gov/entity-information/v4/exclusions?api_key={KEY}&exclusionType=Individual&updatedDate=[MM/DD/YYYY,MM/DD/YYYY]&page=0&size=100
```

### Authentication
**Required.** Free personal API key from `https://sam.gov/profile/details`. Pass as query parameter `api_key={KEY}`.

### Rate Limits
- **Non-federal personal key:** 1,000 requests/day — sufficient for incremental updates
- **Non-federal no key:** 10 requests/day — do not use unauthenticated
- JSON responses: up to 10,000 records per request (paginated)
- CSV extract: up to 1,000,000 records per async extract request

### Incremental Strategy
- **Initial load:** Use CSV extract endpoint (async delivery) with `exclusionType=Individual`. This returns all individual exclusions in a single file.
- **Ongoing:** Query with `updatedDate` filter for records updated since last run. The `updatedDate` parameter accepts a date range `[start,end]`.
- **Dedup:** Store last successful run date as cursor; query `updatedDate=[last_run_date, today]`.

### File Format
- **API (JSON):** Paginated; `entityData` array per page; `totalRecords` for pagination math
- **CSV extract:** Async; returns a download URL via email or polling
- Key fields: `exclusionDetails.exclusionName.firstName`, `.lastName`, `.middleName`, `exclusionDetails.classification` (Individual/Firm/Vessel etc.), `exclusionDetails.exclusionProgram`, `exclusionDetails.activationDate`, `exclusionDetails.terminationDate`

### Parse Targets
- Relationship: `sam_excluded`
- Filter `exclusionDetails.classification = "Individual"`
- Source identifier: SAM-assigned `exclusionDetails.samNumber` or composite name + `activationDate`

### Gotchas
- V1–V3 APIs are dead (retired Sept 2024); use only V4 — do not reference any older documentation
- The `updatedDate` filter range format is `MM/DD/YYYY,MM/DD/YYYY` (comma-separated dates in square brackets as URL parameter)
- CSV async extract requires polling a status endpoint or awaiting an email notification; implement the polling approach
- SAM.gov exclusions overlap with but are not identical to OIG LEIE; both should be captured — different programs, different exclusion lists

---

## 14. EPA ECHO

### Permissibility
**Explicitly permitted via documented public API.** EPA ECHO provides web services specifically designed for custom application development. robots.txt blocks Drupal admin and user paths but not API endpoints. No authentication required.

### Access Method
REST API (HTTP GET). Multiple service groups: facility search, compliance, enforcement cases, penalty data. The enforcement case service is most relevant for named individuals.

**Canonical URLs:**
```
# ECHO Web Services documentation
https://echo.epa.gov/tools/web-services

# Enforcement Case Search (returns enforcement actions including named defendants)
https://echodata.epa.gov/echo/case_rest_services.get_cases?output=JSON&p_case_category=CAA,CWA,RCRA,SDWA&p_case_status=A&qcolumns=1,2,3,4,5,6,7,8,9,10

# Case detail (defendants list per case)
https://echodata.epa.gov/echo/case_rest_services.get_case_info?output=JSON&p_id={case_id}

# Enforcement cases with named individual defendants (p_defendant_type=I for individual)
https://echodata.epa.gov/echo/case_rest_services.get_cases?output=JSON&p_defendant_type=I

# ECHO data downloads (bulk CSV files — alternative to API)
https://echo.epa.gov/tools/data-downloads
```

### Authentication
None required.

### Rate Limits
Not explicitly documented. Treat conservatively: 2 req/sec, sequential. For bulk initial load, use the data download files rather than API pagination.

### Incremental Strategy
- **Initial load:** Use bulk data downloads (`https://echo.epa.gov/tools/data-downloads`) — these are pre-generated CSV files covering all enforcement cases. Filter for individual defendants at parse time.
- **Ongoing:** Query enforcement cases API with date filter (`p_case_activity_date_begin`) for cases with activity since last run. Store last run date as cursor.
- **Dedup:** Store ECHO case ID (`p_id`) for all processed cases.

### File Format
- API: JSON
- Bulk downloads: CSV/ZIP
- Key fields in enforcement cases: `case_number`, `case_name`, `defendant_name`, `defendant_type` (I = Individual, C = Company), `program_acronym`, `penalty_assessed_amt`, `case_activity_date`

### Parse Targets
- Relationship: `epa_enforcement_defendant`
- Filter `defendant_type = "I"` (Individual)
- Individual name from `defendant_name`
- Source identifier: ECHO `case_number` (stable)

### Gotchas
- Named individual defendants are a fraction of all cases; most are corporate defendants — use `p_defendant_type=I` filter to reduce volume
- EPA enforcement data spans multiple programs (CAA, CWA, RCRA, SDWA, TSCA, FIFRA); use bulk download for completeness rather than program-by-program API calls
- `defendant_name` format varies: some are "LAST, FIRST", some are "FIRST LAST", some include "DBA [business]" — normalize during parse
- Bulk download files may use different column names than API responses; verify against actual downloaded files

---

## Common Implementation Requirements

### Shared Utilities Needed

```python
# 1. HTTP client with retry + rate limiting
# All scrapers use httpx with:
# - Automatic retry on 429/5xx (tenacity)
# - Per-domain rate limiting (asyncio.Semaphore or token bucket)
# - Required headers (User-Agent for EDGAR; Authorization for LDA; api_key for SAM)

# 2. R2 upload helper
# Upload raw file to R2 with path: raw/{source_id}/{period}/{filename}
# Return r2_key; compute SHA-256 before upload

# 3. PostgreSQL bulk loader
# psycopg2 COPY for initial loads (>10K rows)
# asyncpg executemany for incremental updates (<10K rows)

# 4. File hash dedup
# SHA-256 of raw bytes before upload; compare to scrape_state.cursor

# 5. Name normalization (applied at ingest AND at query time)
# NFKD unicode → lowercase → strip punctuation → collapse whitespace
```

### Additional Python Dependencies

The following should be added to the project dependencies beyond what was previously listed:

```
openpyxl       # FDA Debarment XLSX parsing
```

### Open Questions Requiring Verification

1. **ATF FFL** — Before implementing, make one GET request to `https://www.atf.gov/firearms/listing-federal-firearms-licensees` to confirm monthly files are directly downloadable without login. Check `robots.txt`.

2. **FDA Debarment** — Fetch the debarment page once to find current XLSX download URLs. Confirm whether OpenFDA covers all debarment categories or only drug product applications.

3. **USASpending archive URLs** — Verify current filenames on `https://www.usaspending.gov/download_center/award_data_archive` before coding fixed URLs. File naming convention includes date stamps that change.

4. **CMS Open Payments download URLs** — Visit `https://openpaymentsdata.cms.gov/` to retrieve stable download URLs for PY2015–PY2024. These are not guaranteed to be at predictable paths.

5. **EDGAR User-Agent email** — Decide on the email address to include in the required `User-Agent` header for all EDGAR requests before running any EDGAR scraper.

6. **SAM.gov API key** — Register for a personal API key at `https://sam.gov/profile/details` before SAM scraper development begins.

7. **LDA API key** — Register at `https://lda.senate.gov/api/register/` before LDA scraper development begins.
