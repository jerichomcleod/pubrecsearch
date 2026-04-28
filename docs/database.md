# Database

PostgreSQL, running locally. The schema is in `db/schema.sql` and applied once via `pubrecsearch init-db`.

---

## Schema

### `individuals`

Stores one row per extracted named person. There is **no deduplication across sources** — the same real-world person named "John Smith" in FEC and OFAC will have two separate rows. See the design rationale in `overview.md`.

```sql
CREATE TABLE individuals (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,         -- original name as extracted
    name_norm       TEXT NOT NULL,         -- normalized form (see normalize.md)
    aliases         JSONB,                 -- ["Alternate Name", ...]  (used by OFAC)
    merge_group_id  BIGINT,                -- NULL; reserved for future entity resolution
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX individuals_fts_idx  ON individuals USING GIN (to_tsvector('english', name));
CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);
```

**`name`:** The name exactly as extracted from the source — preserves original capitalization and punctuation. Examples: `"JOHN A. SMITH"`, `"Smith, John"`, `"John Smith Jr."`.

**`name_norm`:** The canonical normalized form used for search and deduplication within a batch (see the normalization section below). Examples: `"john a smith"`, `"smith john"`, `"john smith jr"`.

**`aliases`:** Only populated by the OFAC SDN scraper, which extracts alternate name spellings from the `<akaList>` XML element. Stored as a JSON array of strings. All other sources leave this NULL.

**`merge_group_id`:** Reserved. Not populated by any scraper. Intended for a future entity-resolution pass that would group rows representing the same real person.

**Indexes:**
- `individuals_fts_idx`: GIN full-text search index used for `tsvector` matching. Not currently used by the search API (which uses trigram similarity instead), but available for future full-text queries.
- `individuals_trgm_idx`: GIN trigram index on `name_norm`. This is the primary search index. Required by the `%` operator (similarity) and the `pg_trgm` extension. Must be created before any search queries will perform acceptably.

### `documents`

One row per raw source file stored in R2.

```sql
CREATE TABLE documents (
    id          BIGSERIAL PRIMARY KEY,
    r2_key      TEXT NOT NULL UNIQUE,      -- "raw/{source_id}/{period}/{filename}"
    source_id   TEXT NOT NULL,             -- e.g. "ofac_sdn", "fec_contributions"
    source_url  TEXT,                      -- original download URL (may be NULL for API calls)
    captured_at TIMESTAMPTZ NOT NULL,      -- timestamp of the download
    file_hash   TEXT NOT NULL,             -- SHA-256 hex of the raw file bytes
    file_size   BIGINT,                    -- bytes
    doc_type    TEXT,                      -- "csv", "xml", "xlsx", "html", "zip", "json"
    period      TEXT                       -- "2024-Q1", "2025-04", "2025-04-17", etc.
);

CREATE INDEX documents_source_idx ON documents (source_id, captured_at DESC);
CREATE INDEX documents_hash_idx   ON documents (file_hash);
```

**`r2_key`:** The canonical R2 object path. Used by the API to generate presigned download URLs. Format: `raw/{source_id}/{period}/{filename}`.

**`file_hash`:** SHA-256 of the raw bytes as downloaded. This is the deduplication key: if two downloads produce the same hash, the second is skipped entirely. For files that are full replacements (OFAC, FARA, OIG LEIE), this means "unchanged file = skip parsing".

**`period`:** A human-readable label for when the data is from, not a strict date. Format varies by source:
- `"2025-04-27"` (daily snapshots: OFAC, FARA)
- `"2025-04"` (monthly: ATF FFL, OIG LEIE)
- `"2024"` (annual: IRS 990 batches, CMS)
- `"2024-Q1"` (quarterly: FEC, EDGAR)
- `"listing"` (DOJ bootstrap listing pages)

**`captured_at`:** Set to `now()` at insert time — the timestamp when the runner downloaded and stored the file, not when the source data was published.

### `individual_documents`

The linkage table connecting individuals to the documents they appear in.

```sql
CREATE TABLE individual_documents (
    individual_id   BIGINT NOT NULL REFERENCES individuals(id),
    document_id     BIGINT NOT NULL REFERENCES documents(id),
    relationship    TEXT NOT NULL,  -- "donor", "sanctioned_individual", "lda_lobbyist", etc.
    excerpt         TEXT,           -- human-readable snippet explaining the connection
    identifiers     JSONB,          -- source-specific structured metadata
    PRIMARY KEY (individual_id, document_id, relationship)
);
```

**`relationship`:** A string identifying the role the individual plays in the document. Each source defines its own relationship type(s):

| Source | Relationship types |
|--------|-------------------|
| OFAC SDN | `sanctioned_individual`, `sanctioned_individual_alias` |
| ATF FFL | `ffl_licensee` |
| FDA Debarment | `fda_debarred_individual` |
| SAM.gov | `sam_excluded_individual` |
| OIG LEIE | `oig_excluded_individual` |
| FEC | `fec_donor` |
| SEC EDGAR | `sec_insider`, `sec_beneficial_owner` |
| IRS Form 990 | `irs990_officer` |
| CMS Open Payments | `cms_payment_recipient` |
| USASpending | `usaspending_recipient` |
| LDA | `lda_lobbyist`, `lda_registrant_contact` |
| FARA | `fara_foreign_agent`, `fara_short_form_filer` |
| DOJ Press | `doj_defendant`, `doj_subject` |
| EPA ECHO | `epa_enforcement_defendant` |

**`excerpt`:** A human-readable text snippet explaining the connection. Format varies by source but is always a string:
- FEC: `"Donor | $2,500 | 2024-03-15 | employer | occupation"`
- OFAC: `"OFAC SDN programs: SDN, IRAN"`
- DOJ: `"Two Men Sentenced for Wire Fraud | defendant John Smith, 45, of Chicago..."`

**`identifiers`:** A JSONB object with source-specific structured metadata. This is the machine-readable counterpart to `excerpt`. Common fields:
- FEC: `{"state": "CA", "employer": "Acme Corp", "total_amount": "5000.00", "contribution_count": "3"}`
- OFAC: `{"ofac_uid": "12345", "programs": ["SDN", "IRAN"], "dob": "1975-01-01"}`
- LDA: `{"filing_uuid": "...", "filing_type": "Q2", "registrant_name": "Acme Lobbying", "income": "100000"}`
- DOJ: `{"url": "https://...", "published": "2025-04-15", "confidence": "high"}`

**Primary key:** `(individual_id, document_id, relationship)` — not `(individual_id, document_id)`. This is intentional: the same person can appear in the same document in multiple roles. Example: an OFAC entry creates both a `sanctioned_individual` row (primary name) and one `sanctioned_individual_alias` row per alias — all linked to the same document.

**`ON CONFLICT DO NOTHING`:** All inserts use this. Re-running a scraper for the same document will not create duplicate rows.

### `scrape_jobs`

Permanent audit log of every scrape run. Never truncated.

```sql
CREATE TABLE scrape_jobs (
    id                  BIGSERIAL PRIMARY KEY,
    source_id           TEXT NOT NULL,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL CHECK (status IN ('running','success','partial','failed')),
    records_processed   INTEGER NOT NULL DEFAULT 0,
    records_new         INTEGER NOT NULL DEFAULT 0,
    errors_count        INTEGER NOT NULL DEFAULT 0,
    notes               TEXT
);
```

**`status`:** Constrained to: `running` (in progress), `success` (0 errors), `partial` (errors but some records processed), `failed` (0 records or discover failed).

**`records_processed`:** Total records seen (including duplicates). For bulk COPY sources this is the number of rows in the staging table. For per-row sources this is the number of `ParsedRecord` objects.

**`records_new`:** New `individual_documents` rows inserted. Lower than `records_processed` for sources with frequent updates of the same individuals. For the first-ever run of a source this equals `records_processed`.

**`notes`:** Populated only on failures, with a brief description of what went wrong (e.g., `"discover failed: HTTP 404"`).

### `scrape_state`

One row per source. Stores the incremental cursor and any extra state the scraper needs.

```sql
CREATE TABLE scrape_state (
    source_id       TEXT PRIMARY KEY,
    last_run_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    cursor          TEXT,    -- source-specific: date, batch ID, file hash, etc.
    metadata        JSONB    -- any extra state the scraper needs between runs
);
```

**`cursor`:** Updated only after a `success` run. Format varies by source (see pipeline.md). On the first run for a source, this is NULL, and `discover()` must handle that case (usually by looking back a default window or returning the most recent available data).

**`metadata`:** Used for state that doesn't fit in a single cursor string. Bootstrap scripts store their progress here (e.g., `{"bootstrap_completed_years": [2015, 2016, 2017]}`). The IRS 990 scraper stores the last batch ID. This column is preserved across runs — the runner reads it at the start of `discover()` and writes it back via `set_state()`.

### `scrape_errors`

Structured per-record error log. Never truncated.

```sql
CREATE TABLE scrape_errors (
    id      BIGSERIAL PRIMARY KEY,
    job_id  BIGINT REFERENCES scrape_jobs(id),
    level   TEXT NOT NULL CHECK (level IN ('warning','error','critical')),
    message TEXT NOT NULL,
    context JSONB,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**`level`:** Three levels:
- `warning`: Per-record failure (one row couldn't be inserted; others continue)
- `error`: Per-target failure (one file couldn't be fetched or parsed; other targets continue)
- `critical`: Entire file's parse failed (no records extracted from that target)

**`context`:** JSON blob with debugging context: the URL, error message, stack trace, source identifier, etc.

---

## Name Normalization

**The most important invariant in the entire system:** `normalize_name()` must be applied identically at ingest time and at query time. If the normalization function changes, all existing `name_norm` values become inconsistent with new ingest and search results break.

The function is in `src/pubrecsearch/normalize.py`:

```python
def normalize_name(name: str) -> str:
    # 1. Unicode NFKD normalization + strip combining characters (removes accents)
    normalized = unicodedata.normalize("NFKD", name)
    normalized = _STRIP_COMBINING.sub("", normalized)
    # 2. Lowercase
    normalized = normalized.lower()
    # 3. Strip punctuation except hyphens/apostrophes within words
    normalized = _PUNCT.sub(" ", normalized)
    # 4. Collapse whitespace
    normalized = _MULTI_SPACE.sub(" ", normalized).strip()
    return normalized
```

**Examples:**
- `"JOHN A. SMITH"` → `"john a smith"` (punctuation stripped, lowercased)
- `"Müller, Hans"` → `"muller hans"` (accent removed, comma stripped)
- `"O'Brien"` → `"o'brien"` (apostrophe within word preserved)
- `"Smith-Jones"` → `"smith-jones"` (hyphen within word preserved)
- `". ."` → `""` (placeholder name → empty string)

**Placeholder name filtering:** Some government datasets contain placeholder names like `". ."`, `"."`, `" "`. These normalize to an empty string. Two places filter them:
1. Inside `bulk_insert_individuals()`: rows where `name_norm` is empty are dropped before the COPY
2. Inside CMS and other scrapers: rows with empty normalized names are skipped before building row dicts

**Gotcha:** The `_PUNCT` regex strips punctuation except for hyphens and apostrophes. However, the regex is `[^\w\s\-']` — it preserves ALL hyphens and apostrophes, not just word-internal ones. So `"-Smith"` normalizes to `"-smith"` and `"O' Brien"` normalizes to `"o' brien"`. This is consistent behavior, but it means names with leading hyphens or isolated apostrophes are not stripped clean.

---

## PostgreSQL Extensions

The schema requires one extension:

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

`pg_trgm` provides:
1. The `similarity(a, b)` function — returns a float 0.0–1.0 (used in `/search`)
2. The `%` operator — true if `similarity(a, b) > pg_trgm.similarity_threshold` (default 0.3)
3. Support for `GIN` indexes on trigram decompositions (`gin_trgm_ops`)

The search API uses `WHERE name_norm % $query` (operator) filtered by minimum similarity. The GIN index on `name_norm gin_trgm_ops` makes this fast even across millions of rows.

---

## Bulk COPY Details

For large sources, PostgreSQL `COPY` is used via Python's `psycopg2`. The flow:

### TSV Escaping

All values are escaped for PostgreSQL's tab-separated COPY format:

```python
def _pg_escape(s: str) -> str:
    if s is None or s == "":
        return r"\N"   # PostgreSQL NULL
    return s.replace("\\", "\\\\")   # escape backslash first
             .replace("\t", "\\t")   # escape tab
             .replace("\n", "\\n")   # escape newline
             .replace("\r", "\\r")   # escape carriage return
```

**Assumption:** Empty strings and None both map to `\N` (PostgreSQL NULL). This means a row with `excerpt=""` will be stored as NULL in the staging table, not as an empty string. After the CTE INSERT, the `excerpt` column in `individual_documents` will be NULL for those rows.

### Batch Sizing

Large scrapers process rows in batches (typically 10K–500K rows per `bulk_insert_individuals()` call) to:
1. Limit peak RAM usage (each batch is a StringIO buffer)
2. Allow partial progress if a batch fails
3. Avoid PostgreSQL temp table size limits

The FEC bootstrap uses 500K-row batches. LDA and EPA use 10K-row batches.

---

## Index Maintenance

When running historical bootstrap scripts for large sources (FEC has 50–100M rows per cycle), dropping and rebuilding the trigram index is significantly faster than maintaining it live:

```sql
-- Before bulk load
DROP INDEX IF EXISTS individuals_trgm_idx;

-- ... COPY millions of rows ...

-- After bulk load
CREATE INDEX individuals_trgm_idx ON individuals USING GIN (name_norm gin_trgm_ops);
```

The bootstrap scripts do not currently automate this — it's documented here as a manual step for operators running the first full backfill. For ongoing incremental inserts (a few thousand rows per run), maintaining the index live is acceptable.

---

## Useful Queries

The `scripts/explore_db.ipynb` Jupyter notebook contains ready-to-run cells for common queries. Key patterns:

```sql
-- Count by source
SELECT d.source_id, COUNT(DISTINCT id.individual_id) AS individuals
FROM individual_documents id JOIN documents d ON d.id = id.document_id
GROUP BY d.source_id ORDER BY individuals DESC;

-- Fuzzy name search
SELECT i.name, round(similarity(i.name_norm, 'john smith')::numeric, 3) AS score,
       d.source_id, id_link.relationship, id_link.excerpt
FROM individuals i
JOIN individual_documents id_link ON id_link.individual_id = i.id
JOIN documents d ON d.id = id_link.document_id
WHERE similarity(i.name_norm, 'john smith') > 0.4
ORDER BY score DESC LIMIT 20;

-- Cross-source: individuals appearing in 2+ sources
SELECT i.name,
       COUNT(DISTINCT d.source_id) AS source_count,
       array_to_string(array_agg(DISTINCT d.source_id ORDER BY d.source_id), ', ') AS sources
FROM individuals i
JOIN individual_documents id_link ON id_link.individual_id = i.id
JOIN documents d ON d.id = id_link.document_id
GROUP BY i.id, i.name
HAVING COUNT(DISTINCT d.source_id) >= 2
ORDER BY source_count DESC;
```
