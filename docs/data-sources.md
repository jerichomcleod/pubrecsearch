# Data Sources

This document covers all 14 data sources: what they contain, how they are accessed, what is extracted, and all known quirks and assumptions.

---

## Phase 1 — Small Bulk Sources

These five sources produce small files (a few MB to ~30 MB) with manageable row counts. They use the standard `fetch()` + per-row `parse()` path. No streaming, no bulk COPY.

---

### 1. OFAC Specially Designated Nationals (`ofac_sdn`)

**What it is:** The U.S. Treasury's list of sanctioned individuals and entities. Maintained by the Office of Foreign Assets Control. Includes foreign nationals, terrorists, narcotics traffickers, and others. Presence on this list makes it illegal for U.S. persons to transact with these individuals.

**Access:** Direct download at a fixed URL. Full list replaced daily. No authentication required.

```
https://www.treasury.gov/ofac/downloads/sdn_xml.zip
```

**Format:** ZIP containing `sdn.xml` (~25–30 MB uncompressed). XML with namespace. Key elements: `<sdnEntry>`, `<sdnType>`, `<uid>`, `<lastName>`, `<firstName>`, `<akaList>`, `<dateOfBirthList>`, `<programList>`.

**Schedule:** Daily at 6 AM.

**Relationship types:**
- `sanctioned_individual` — the primary name
- `sanctioned_individual_alias` — each alias from `<akaList>`

**What is extracted:**
- Primary name (first + last)
- All aliases (each gets its own `individuals` row)
- Sanctions programs (e.g., `["SDN", "IRAN", "NPWMD"]`)
- Date of birth (when present)
- OFAC UID (stable numeric identifier)

**Dedup strategy:** Full list replacement. SHA-256 of the downloaded ZIP is stored. If the hash matches the last run, the entire file is skipped.

**Assumptions:**
- The XML uses a namespace on all elements. The namespace URI is detected from the root tag (`{ns}sdnEntry` vs bare `sdnEntry`) and applied consistently.
- Aliases are treated as separate individuals with `relationship="sanctioned_individual_alias"` and `identifiers.primary_name` pointing back to the main entry's UID. This is intentional — searching for an alias returns the alias row, which has the primary name in its identifiers.
- Filter: only `<sdnType>Individual</sdnType>` entries are processed. Organizations, vessels, and aircraft are skipped.
- OFAC doesn't guarantee stable URLs for its other download files (sdn.csv, etc.) — only the ZIP URL above is relied upon.

---

### 2. ATF Federal Firearms Licensee List (`atf_ffl`)

**What it is:** All active Federal Firearms Licensees in the United States — licensed gun dealers, manufacturers, collectors, and importers. Published monthly by the Bureau of Alcohol, Tobacco, Firearms and Explosives.

**Access:** Monthly form-based download from ATF's website. The page requires a session cookie and CSRF token (form_build_id); the actual CSV URL changes each session.

**URL:** `https://www.atf.gov/firearms/tools-and-services-firearms-industry/federal-firearms-listings`

**WAF bypass required:** The ATF website uses Akamai Bot Manager, which blocks all automated HTTP clients by fingerprinting the TLS handshake. Standard `httpx`, `requests`, and even headless Playwright are blocked. The scraper uses `curl_cffi` with `impersonate="chrome124"`, which uses BoringSSL (Chrome's TLS library) to produce an indistinguishable TLS fingerprint.

**Flow:**
1. GET page with `curl_cffi` Session — receives session cookie and HTML with `form_build_id`
2. POST form with `year`, `month`, `form_build_id` → response HTML contains a download link
3. GET the download link — receives the CSV

**Format:** Pipe-delimited CSV (`|`). No header row — columns are positional per ATF documentation. Encoding: UTF-8 with latin-1 fallback. Size: ~10–15 MB/month, ~128K rows.

**Schedule:** 1st of each month at 4 AM.

**Relationship type:** `ffl_licensee`

**What is extracted:** Only sole proprietors (individuals). The filter uses:
- License type must be in `{01, 02, 03, 06, 07, 08, 09, 10, 11}` — covering dealers, collectors, and manufacturers
- Business name must not match entity patterns: LLC, Corp, Inc, Trust, Foundation, Association, etc.

**Assumptions:**
- ATF only makes data available through the prior month. The current month is never available. `discover()` probes up to 4 months back to find the most recent available file.
- The form ID `ffl-complete-export-form` and the `Apply` button label are assumed stable. If ATF changes its form, the scraper will break.
- The CSV has no header row. Column positions are hardcoded per ATF's published data dictionary.
- Month offsets are computed via `monthrange()` to handle leap years.
- `curl_cffi` is an optional dependency. If not installed, the scraper raises `ImportError`.

---

### 3. FDA Debarment List (`fda_debarment`)

**What it is:** Individuals and firms debarred from participating in the development or approval of drug applications. Maintained by the Food and Drug Administration. Debarment is a consequence of felony convictions related to drug regulation.

**Access:** HTML page with embedded tables. No structured download endpoint.

**URL:** `https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/fda-debarment-list-drug-product-applications`

**Format:** HTML tables (BeautifulSoup). Each row is one debarred individual with: name, effective date, debarment period, statutory basis.

**Schedule:** Weekly, Monday at 7 AM.

**Relationship type:** `fda_debarred_individual`

**What is extracted:**
- Name (from name column, filtered for non-company entries)
- Effective date
- Debarment period (e.g., "3 years", "permanent")
- Statutory basis (e.g., "21 U.S.C. § 335a(a)")

**Dedup strategy:** SHA-256 of the full HTML page. Re-parsed only when the page content changes.

**Assumptions:**
- The scraper tries multiple column name patterns to find the name/date/basis columns (case-insensitive keyword matching). If FDA restructures their HTML, column detection may fail silently.
- Company entries are filtered using a regex: skips rows where the name contains LLC, Corp, Inc, Ltd, Co., Company, Corporation, Group.
- Name length must be 4–60 characters to avoid picking up page navigation text.
- The FDA maintains multiple separate debarment lists (food import, drug import, tobacco). Only the drug product applications list is currently scraped.

---

### 4. SAM.gov Exclusions (`sam_exclusions`)

**What it is:** Entities and individuals excluded from receiving federal contracts or assistance. SAM (System for Award Management) is the government's contractor database. Exclusions are added for causes including debarment, suspension, and voluntary exclusion.

**Access:** REST API v4. Requires a free SAM.gov API key. The key is a personal key with a hard limit of **10 API requests per day** for non-federal users.

**URL:** `https://api.sam.gov/entity-information/v4/exclusions`

**Flow (async CSV extract):**
1. GET `/exclusions?api_key={KEY}&exclusionType=Individual&format=csv` — response body contains a token URL (not JSON)
2. Poll the token URL until the CSV is ready (404 = still processing, JSON object = status, CSV bytes = ready)
3. Parse the downloaded CSV

**Format:** CSV with header row. Encoding: UTF-8-BOM (byte order mark). Size: ~10–20 MB.

**Schedule:** Daily at 5 AM. **Rate-limited:** `discover()` returns empty if `cursor == today` (already processed today's data).

**Relationship type:** `sam_excluded_individual`

**What is extracted:**
- Name (First + Middle + Last, or UEI entity name)
- UEI (Unique Entity Identifier) / SAM number
- Classification (Individual, Firm, etc.) — only Individual rows are kept
- Exclusion type, activation date, termination date
- Agency that imposed the exclusion

**Assumptions:**
- SAM v1–v3 APIs were retired in September 2024. Only v4 is used.
- The token URL is extracted via regex from the raw response text (`re.search(r'https://[^\s"]+', text)`), not JSON parsing — the SAM API returns the token URL embedded in a plain-text message or wrapped in non-standard JSON.
- The poll interval increases with each attempt: `time.sleep(10 * attempt)`, capped at 60 seconds. Max 300 total seconds of polling.
- Column name normalization: SAM CSV column names are lowercased and spaces replaced with underscores before lookup. Both naming conventions (e.g., `First` and `first_name`) are tried.
- The 10 req/day limit means: one successful run per day. If a run is interrupted mid-poll, the next attempt the same day may fail. The cursor-based daily skip prevents wasted requests.

---

### 5. OIG LEIE (`oig_exclusions`)

**What it is:** The HHS Office of Inspector General's List of Excluded Individuals and Entities. Covers healthcare providers excluded from participation in Medicare, Medicaid, and other federal health programs — typically due to fraud, patient abuse, or criminal convictions.

**Access:** Fixed URL, file replaced monthly.

**URL:** `https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv`

**Format:** CSV with header row. Uppercase column names. Encoding: UTF-8. Size: ~30 MB, ~79K rows.

**Schedule:** 8th of each month at 3 AM. OIG typically updates in the first week of the month; the 8th ensures the fresh file is available.

**Relationship type:** `oig_excluded_individual`

**What is extracted:**
- Name (FIRSTNAME + MIDNAME + LASTNAME)
- NPI (National Provider Identifier) — present for most providers
- DOB, state, specialty
- Exclusion type (mandatory vs. permissive)
- Exclusion date, reinstatement date (if applicable)
- UPIN (legacy identifier, pre-dates NPI)

**Dedup strategy:** SHA-256 of the CSV. Re-parsed only on change.

**Assumptions:**
- OIG uses uppercase column names: `LASTNAME`, `FIRSTNAME`, `MIDNAME`, `NPI`, `EXCDATE`. The scraper reads them as-is.
- Filter: rows where `LASTNAME` is populated are individuals. Entities (companies, hospitals) have a `BUSNAME` but no `LASTNAME`. There is some overlap — some entity exclusions also have a last name field. The current filter (`LASTNAME is not null`) may include non-individuals.
- `REINSTATE_DATE` is retained in identifiers for excluded individuals who were later reinstated. These historical records are valuable even though the exclusion is lifted.
- NPI is absent for exclusions predating the NPI system (pre-2007). The scraper stores whatever identifiers are available.

---

## Phase 2 — Large Bulk Sources

These five sources involve files too large or too numerous for per-row processing. They use the `parse_to_db()` bulk COPY path, and the largest (CMS) also uses `fetch_to_file()` streaming.

---

### 6. FEC Individual Contributions (`fec_contributions`)

**What it is:** Individual campaign contributions reported to the Federal Election Commission. Includes all contributions to federal campaigns and PACs by individual donors.

**Access:** Bulk ZIP files on AWS GovCloud S3 — one ZIP per 2-year election cycle.

**URL pattern:** `https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com/bulk-downloads/{YEAR}/indiv{YY}.zip`

**Format:** Pipe-delimited (`|`). No header row — columns are positional per FEC data dictionary. Encoding: UTF-8. Size: ~3–5 GB compressed, ~15–20 GB uncompressed per 2-year cycle.

**Schedule:** Quarterly, 1st of the quarter month at 2 AM. FEC publishes incremental additions continuously, but the bulk ZIP is the most efficient load mechanism.

**Relationship type:** `fec_donor`

**What is extracted:**
- Name (from `NAME` field: `LAST, FIRST MIDDLE` format — stored as-is)
- State, city/zip
- Employer, occupation
- Total contribution amount (aggregated per unique name+state combination)
- Contribution count

**Key implementation detail:** The FEC ZIP contains millions of rows — one per individual transaction. The scraper uses Polars lazy scanning to:
1. Filter `ENTITY_TP = "IND"` (individual donors only; excludes PACs and businesses)
2. Group by `(name, state)` and aggregate `total_amount` and `contribution_count`
3. This reduces millions of transactions to thousands/millions of unique donors per cycle

**Assumptions:**
- Cycle file naming: `indiv20.zip` covers the 2019–2020 cycle. The year in the filename is always even and matches the even year of the cycle.
- The `NAME` field is in `LAST, FIRST MIDDLE` format (not standardized — some entries don't follow this). The name is stored as-is, not restructured to `FIRST LAST`.
- No header row. Column positions are hardcoded. FEC occasionally changes the schema between cycles; the positions used are current as of 2024.
- `ENTITY_TP = "IND"` is the sole filter for individual donors. PAC donations (`COM`), party contributions (`PAR`), etc. are skipped.
- ETag/Last-Modified dedup: before downloading, the runner does a HEAD request to check if the file has changed since the last successful run.

---

### 7. SEC EDGAR (`sec_edgar`)

**What it is:** SEC's Electronic Data Gathering, Analysis, and Retrieval system. Contains all public company filings. The scraper targets Form 4 (insider transactions), Form 3 (initial insider holdings), SC 13G, and SC 13D (large shareholder disclosures).

**Access:** Free public API. Requires a `User-Agent` header with organization name and contact email. SEC blocks requests without this header.

**URL pattern:**
```
https://www.sec.gov/Archives/edgar/full-index/{YEAR}/QTR{1-4}/form.idx     (quarterly)
https://www.sec.gov/Archives/edgar/daily-index/{YEAR}/QTR{1-4}/form{MMDDYYYY}.idx  (daily)
```

**Format:** Fixed-width text, compressed with gzip (`.idx.gz` for daily). Columns: Form Type (0–20), Company Name (20–82), CIK (82–92), Date Filed (92–102), Filename (102+).

**Schedule:** Weekly, Monday at 3 AM. Downloads daily index files for the past 7 days.

**Relationship types:**
- `sec_insider` — Form 4 and Form 3 filers (corporate insiders)
- `sec_beneficial_owner` — SC 13G/D filers (large shareholders, >5%)

**What is extracted:**
- Name (from the "Company Name" field of the index — for Forms 3/4, this is the reporting person's name, not the company)
- CIK (SEC's numeric entity identifier)
- Accession number (derived from filename path)
- Date filed
- Form type

**Assumptions:**
- In the EDGAR daily index, the "Company Name" column for Form 4/3 filings is the reporting owner's name, not the issuer. This is specific to these form types — for most other forms, "Company Name" is the filer company.
- Institutional filers (for SC 13G/D) are filtered using entity patterns: if the name contains LP, LLC, Corp, Inc, Trust, Fund, Partners, Management, Capital, Bank, etc., it's skipped as a non-individual.
- Rate limit: SEC enforces a hard 10 req/sec limit. The scraper uses sequential requests with no explicit delay but should stay well under this limit for weekly incremental runs.
- Daily index files are checked via HEAD before download to avoid unnecessary fetches on weekends/holidays when no new files are published.
- Lines shorter than 102 characters in the fixed-width format are skipped as malformed.

---

### 8. IRS Form 990 (`irs_990`)

**What it is:** Annual information returns filed by tax-exempt organizations. Contains officer/director compensation tables and organizational details. Covers nonprofits, foundations, universities, hospitals, and other 501(c) organizations.

**Access:** Monthly batch ZIPs from the IRS TEOS (Tax-Exempt Organizations) system.

**URL pattern:**
```
https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/index_{YEAR}.csv
https://apps.irs.gov/pub/epostcard/990/xml/{YEAR}/{BATCH_ID}.zip
```

**Important:** The old S3 bucket (`s3://irs-form-990/`) was decommissioned December 31, 2021 and is no longer updated. All code uses the TEOS URL above.

**Format:** Batch ZIPs each containing 5K–15K individual XML files. Each XML is one 990 return. Size per batch ZIP: ~50–300 MB.

**Schedule:** 10th of each month at 6 AM.

**Relationship type:** `irs990_officer`

**What is extracted:**
- Officer/director/trustee/HCE name from Part VII Section A
- Title
- Compensation amount (from the filing)
- Organization EIN, organization name
- Tax period, form type (990, 990-EZ, 990-PF)

**Batch ID cursor:** Batch IDs are strings like `"2025_TEOS_XML_01A"`, `"2025_TEOS_XML_02B"`. They sort lexicographically in the same order as they were published, so `cursor = last_batch_id` and `batch_id > cursor` correctly identifies new batches.

**XML parsing:** The scraper strips XML namespaces before parsing (regex removes all `{ns}` prefixes from tag names). This handles the multiple namespace variants used across IRS years (990 XML schema has evolved). Key XML paths sought:
- `Form990PartVIISectionAGrp`
- `OfficerDirectorTrusteeKeyEmplGrp`
- `HighestCompensatedEmployeeGrp`

**Assumptions:**
- The IRS publishes 5 new batches per run at most (`_MAX_BATCHES_PER_RUN = 5`) to limit runtime.
- `discover()` falls back to the prior year's index CSV if the current year's index returns 404 (common in January before the year's first batch is published).
- 990-N (e-postcard) filers are not in the XML ZIPs and contain no individual data.
- The batch ZIP is opened in memory (not streamed to disk) because each batch is only 50–300 MB — manageable without streaming.

---

### 9. CMS Open Payments (`cms_open_payments`)

**What it is:** Payments from pharmaceutical and medical device manufacturers to physicians and teaching hospitals, as required by the Sunshine Act. Published annually by the Centers for Medicare & Medicaid Services.

**Access:** Bulk CSV downloads discovered via Socrata metadata API.

**URL:** `https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items`

**Important two-step discovery:** The Socrata metadata list endpoint does not include download URLs. Each dataset must be fetched individually by its identifier:
```
https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items/{identifier}
```
The download URL is then extracted from the `distribution[].downloadURL` field.

**Format:** ZIP containing large CSV files (pipe-delimited, not comma-delimited). Each annual ZIP is 2–4 GB compressed, ~9 GB uncompressed. Uses `fetch_to_file()` streaming path.

**Schedule:** Monthly, 15th at 5 AM. New program year published each June; monthly polling detects it.

**Relationship type:** `cms_payment_recipient`

**What is extracted:**
- Physician name (Covered_Recipient_First/Middle/Last_Name)
- NPI (Covered_Recipient_NPI)
- Specialty
- Payment amount and manufacturer name
- State

**Streaming path:** This is the only source that uses both `fetch_to_file()` (streaming download) and `parse_to_db()` (bulk COPY). The 9 GB file cannot be loaded into RAM. The flow:
1. Stream download to temp file via `client.stream()` with 8 MB chunks
2. Runner hashes the temp file incrementally
3. Runner uploads to R2 via multipart (no full load)
4. `parse_to_db()` reads from temp file path using Polars lazy scanning

**Assumptions:**
- CMS CSV is **pipe-delimited (`|`)**, not comma-delimited. This is documented by CMS but easy to miss.
- Column detection is case-insensitive and tries multiple name variations (e.g., `Covered_Recipient_Last_Name` vs `LastName`).
- Placeholder names (e.g., `. .`, `.`) that normalize to empty are skipped at the row level.
- Program year is extracted from the dataset title (regex: 4-digit year between 2013 and the current year).
- Within the ZIP, the scraper looks for CSV files with both "general" and "detail" in the filename. If none match, it takes the first CSV.
- CMS may refresh prior-year data in January with late submissions. The hash-based dedup handles this: if the file content changed, it re-parses.

---

### 10. USASpending.gov (`usaspending`)

**What it is:** Federal award data — contracts, grants, loans, and other financial assistance. Contains the recipients of government awards. The scraper filters to individual recipients.

**Access:** Async bulk download API.

**URL:** `https://api.usaspending.gov/api/v2/bulk_download/awards/`

**Flow:**
1. POST request with filters → response contains `file_name` token
2. Poll `GET /api/v2/bulk_download/status/?file_name={token}` until `status = "finished"`
3. GET the `file_url` from the status response to download the ZIP

**Filter:** `recipient_type_names: ["Individual"]` — individual persons rather than companies or organizations.

**Format:** ZIP containing multiple CSV files split by size threshold. Encoding: UTF-8.

**Schedule:** Weekly, Tuesday at 4 AM.

**Relationship type:** `usaspending_recipient`

**What is extracted:**
- Recipient name
- Award type (contract, grant, etc.)
- Award amount
- State
- Award ID (FAIN for assistance, PIID for contracts)

**Assumptions:**
- The API filter key is `date_range` (not `time_period` — the old key caused a 422 error). The correct payload structure is:
  ```json
  {"filters": {"recipient_type_names": [...], "date_range": {...}, "prime_award_types": [...]}, "award_levels": ["prime_awards"]}
  ```
- `prime_award_types` must be specified; the API requires it alongside `award_levels`.
- Polling interval: 15 seconds. Max polling time: 10 minutes (40 polls). After that, the target is marked as an error.
- The ZIP may contain multiple CSVs (one per batch threshold). All are parsed together.
- Column names are flexible — the scraper tries multiple candidate names for each field (name, amount, state, award_id).

---

## Phase 3 — Structured API Sources

---

### 11. LDA Lobbying Disclosure (`lda`)

**What it is:** Lobbying activity reports filed with the U.S. Senate under the Lobbying Disclosure Act. Covers organizations and individuals engaged in lobbying Congress and executive branch agencies.

**Access:** REST API v1. Requires a free API key (register at `lda.senate.gov/api/register/`). Rate limit: 120 requests/minute.

**URL:** `https://lda.senate.gov/api/v1/filings/`

**Authentication:** `Authorization: Token {lda_api_key}` header.

**Schedule:** Weekly, Monday at 6 AM. Lobbying filings are submitted quarterly but amendments are filed continuously.

**Relationship types:**
- `lda_lobbyist` — named lobbyists listed on a filing
- `lda_registrant_contact` — registrant contact person (when the name looks like a person, not an organization)

**What is extracted from each filing:**
- Each lobbyist's first/last name
- Filing UUID, type (Q1/Q2/Q3/Q4/amendment), year
- Registrant name and ID
- Income/expenses amounts
- Issue areas (lobbying activity codes)
- Covered government entities

**Pagination:** Each page returns 25 results. Subsequent pages use the `next` URL from the response. Rate limiting: 0.5 second sleep between requests (120 req/min ÷ 2 = 60 req/min with headroom).

**Assumptions:**
- On first run (no cursor), the scraper looks back 90 days to capture the current quarter.
- `_looks_like_person()` uses a blacklist of organization markers (LLC, Inc, Corp, Group, Firm, etc.) to determine whether the registrant contact name is an individual or a company. This is a heuristic — it will miss some individuals named after their company and incorrectly include some company names that don't contain these markers.
- The `next` URL in the API response is used as-is for subsequent pages. The first request uses `?received_dt__gte={date}&page_size=25`; subsequent requests use only the `next` URL (parameters already embedded).
- LDA does not deduplicate lobbyists across amendments — the same person may appear on both the original filing and its amendment. The `ON CONFLICT DO NOTHING` in `individual_documents` prevents duplicate rows.
- Historical bootstrap is handled separately (see bootstrap.md).

---

### 12. FARA (`fara`)

**What it is:** Foreign Agents Registration Act filings. Individuals and organizations that act as agents of foreign principals (governments, political parties, foreign entities) must register. The filings reveal who is lobbying on behalf of foreign interests.

**Access:** Direct download of bulk CSV ZIPs. No authentication required. Files are replaced daily.

**URLs:**
```
https://efile.fara.gov/bulk/zip/FARA_All_Registrants.csv.zip    (all registrant orgs + individuals)
https://efile.fara.gov/bulk/zip/FARA_All_Short_Form.csv.zip     (NSD-6: individuals employed by registrant orgs)
```

**Format:** CSV, ISO-8859-1 (Latin-1) encoding. Comma-delimited. FARA explicitly documents this encoding; opening as UTF-8 silently corrupts non-ASCII names.

**Schedule:** Daily at 4 AM. Both files are downloaded on each run.

**Relationship types:**
- `fara_foreign_agent` — individual registrants (from Registrants CSV, filtered to RegistrantType="Individual")
- `fara_short_form_filer` — individuals employed by registrant organizations (from Short Form CSV; all entries are individuals)

**What is extracted:**
- Name (from split first/last columns if available, or full name column)
- Registrant ID, registration/termination dates
- Country of formation
- State

**Assumptions:**
- Dedup: both files are full daily snapshots. SHA-256 dedup at the file level — if unchanged, nothing is re-processed.
- FARA has historically changed its column names between CSV releases. The scraper uses `_col()` — a case-insensitive function that tries multiple candidate names for each field. If a column isn't found under any candidate name, that field is silently omitted from identifiers.
- Termination date is retained. A terminated registration is historically significant.
- Registrants filter: case-insensitive string-contains check on RegistrantType for "individual".

---

## Phase 4 — HTML Scraping / Enforcement APIs

---

### 13. DOJ Press Releases (`doj_press`)

**What it is:** Defendant names extracted from Department of Justice press releases announcing charges, indictments, guilty pleas, sentences, and convictions. Covers all 94 U.S. Attorney offices and DOJ component agencies.

**Access:** RSS feed for ongoing; HTML listing pagination for historical bootstrap.

**URLs:**
```
https://www.justice.gov/news/press-releases/rss.xml    (RSS — up to ~40 most recent)
https://www.justice.gov/news/press-releases?page={N}   (HTML listing)
https://www.justice.gov/opa/pr/{slug}                  (individual press release HTML)
```

**Schedule:** Daily at 7 AM.

**Relationship types:**
- `doj_defendant` — high-confidence extractions (title patterns)
- `doj_subject` — medium-confidence extractions (body patterns)

**Extraction logic (no LLM):** Two-tier deterministic regex:

**High confidence (title patterns):**
```
"United States v. John Smith"       → doj_defendant
"John Smith Sentenced to 5 Years"   → doj_defendant
"Jane Doe Charged with Wire Fraud"  → doj_defendant
"Robert Johnson Pleads Guilty"      → doj_defendant
"Mary Williams Convicted"           → doj_defendant
"Thomas Brown Indicted"             → doj_defendant
```

**Medium confidence (body patterns):**
```
"defendant John Smith, 45, of Chicago"    → doj_defendant
"John Smith, of Detroit, was charged"    → doj_subject
"sentenced John Smith to 10 years"       → doj_subject
"defendant Robert Davis, age 52"         → doj_subject
```

**Name validation — `_is_plausible_name()`:**
A match is accepted only if:
1. 2–5 tokens, 4–60 characters total
2. First token is not hyphenated (prevents "Co-defendant", "Ex-officer")
3. Each substantive token starts with uppercase
4. No token (including hyphen-split parts) is in the stopword list

**Stopword list includes:**
- Government entities: united, states, federal, district, department, bureau, office, court, judge, attorney, general
- Legal terms: complaint, indictment, conspiracy, fraud, theft, drug, enforcement, securities
- Number words: two, three, four, ..., several, multiple
- Plural group nouns: men, women, people, defendants, officers, agents
- Role prefixes: co, ex, non

**Raw HTML storage:** Each press release HTML page is stored in R2 under `raw/doj_press/{YYYY-MM}/{slug}.html`. The `excerpt` stored in `individual_documents` is the press release title plus the first 500 characters of body text.

**Assumptions:**
- The DOJ website runs Drupal. The main body content is in `.field-items .field-item` or related selectors. If DOJ changes their CMS, the body text extractor will fall back to joining all `<p>` tags.
- The RSS feed returns only the ~40 most recent items. If the scraper misses a run window wider than ~2 days, some press releases may never be processed by the ongoing scraper. The DOJ bootstrap (see bootstrap.md) handles historical recovery.
- `_NAME_RE` allows hyphens in last-name tokens (for "Smith-Jones") but not in the first token (to block "Co-defendant"). This means hyphenated first names like "Jean-Pierre" are not matched — an acceptable false negative given the rarity of such names in US federal press releases.
- Confidence is stored in `identifiers.confidence` (`"high"` or `"medium"`). If the same name is extracted by both a title and a body pattern, the higher confidence level is kept.
- Body text is truncated to 3,000 characters for regex matching to avoid performance issues on very long press releases.

---

### 14. EPA ECHO (`epa_echo`)

**What it is:** EPA enforcement cases against individual defendants. ECHO (Enforcement and Compliance History Online) tracks all federal environmental enforcement actions across programs including Clean Air Act, Clean Water Act, RCRA, TSCA, and others.

**Access:** JSON REST API. No authentication required. Rate limit: ~2 req/sec.

**URLs:**
```
https://echodata.epa.gov/echo/case_rest_services.get_cases     (case list)
https://echodata.epa.gov/echo/case_rest_services.get_case_info  (case detail)
```

**Schedule:** Weekly, Wednesday at 6 AM.

**Relationship type:** `epa_enforcement_defendant`

**Flow:**
1. `get_cases?p_defendant_type=I&p_case_activity_date_begin={date}` → paginated list of case ActivityIds
2. For each ActivityId: `get_case_info?p_id={id}` → full case detail including defendants

**What is extracted:**
- Defendant name
- Case number (e.g., `"CAA-09-2020-001"`)
- Program code (CAA, CWA, RCRA, TSCA, etc.)
- State, city
- Total penalty amount (sum of all `CasePenaltyData` entries)
- Activity date

**Defensive field access:** The ECHO API's JSON response structure uses varying field names across programs and API versions. The scraper uses `_nested_get()` and `_str_field()` helper functions that try multiple key paths for each value. For example, defendants may be in `Results.CaseDefendantData`, `Results.CaseDefendants`, or `defendants` — all three are tried.

**Rate limiting:** 0.5-second sleep between `get_case_info` calls. This limits throughput to ~2 req/sec, consistent with ECHO's documented limit.

**Assumptions:**
- `p_defendant_type=I` filters to individual defendants at the case level. Most EPA enforcement is against corporations; individuals are a small fraction.
- Pagination for the case list: stops when the returned page has fewer than `_CASE_PAGE_SIZE` (100) results, or when the total count is reached, or when results is empty.
- Penalty amounts are summed across all `CasePenaltyData` entries for a case. Some cases have multiple penalties across multiple programs; the stored value is the total.
- ISO dates from the system (YYYY-MM-DD) are converted to the ECHO API format (MM/DD/YYYY) before sending.
- The ECHO API returns corporate defendants alongside individual defendants even when `p_defendant_type=I` is specified at the case level. The parser filters each defendant record individually: `defendant_type` must be `"I"` or `"Individual"` (case-insensitive).
