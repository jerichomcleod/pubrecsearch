-- PubRecSearch PostgreSQL schema
-- Run once on a fresh database: psql -d pubrecsearch -f db/schema.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Named individuals extracted from any source.
-- Option A: no entity resolution at ingest — each source record is stored as-is.
CREATE TABLE IF NOT EXISTS individuals (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    name_norm       TEXT NOT NULL,
    aliases         JSONB,           -- ["Alternate Name", ...]
    merge_group_id  BIGINT,          -- reserved for future entity resolution
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS individuals_fts_idx
    ON individuals USING GIN (to_tsvector('english', name));

CREATE INDEX IF NOT EXISTS individuals_trgm_idx
    ON individuals USING GIN (name_norm gin_trgm_ops);

-- Every raw file stored in R2.
CREATE TABLE IF NOT EXISTS documents (
    id          BIGSERIAL PRIMARY KEY,
    r2_key      TEXT NOT NULL UNIQUE,
    source_id   TEXT NOT NULL,
    source_url  TEXT,
    captured_at TIMESTAMPTZ NOT NULL,
    file_hash   TEXT NOT NULL,   -- SHA-256; used to skip unchanged files
    file_size   BIGINT,
    doc_type    TEXT,            -- "csv", "xml", "xlsx", "html"
    period      TEXT             -- e.g. "2024-Q1", "2025-04-17"
);

CREATE INDEX IF NOT EXISTS documents_source_idx
    ON documents (source_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS documents_hash_idx
    ON documents (file_hash);

-- Individual <-> document linkage with role context.
CREATE TABLE IF NOT EXISTS individual_documents (
    individual_id   BIGINT NOT NULL REFERENCES individuals(id),
    document_id     BIGINT NOT NULL REFERENCES documents(id),
    relationship    TEXT NOT NULL,  -- "donor", "sanctioned_individual", "oig_excluded_individual", etc.
    excerpt         TEXT,           -- relevant snippet or JSON summary
    identifiers     JSONB,          -- {"sub_id": "...", "ein": "...", "fec_id": "..."}
    PRIMARY KEY (individual_id, document_id, relationship)
);

CREATE INDEX IF NOT EXISTS ind_docs_individual_idx
    ON individual_documents (individual_id);

CREATE INDEX IF NOT EXISTS ind_docs_document_idx
    ON individual_documents (document_id);

-- Scrape job audit log. Never truncate — permanent record.
CREATE TABLE IF NOT EXISTS scrape_jobs (
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

CREATE INDEX IF NOT EXISTS scrape_jobs_source_idx
    ON scrape_jobs (source_id, started_at DESC);

-- Per-source incremental cursor / dedup state.
CREATE TABLE IF NOT EXISTS scrape_state (
    source_id       TEXT PRIMARY KEY,
    last_run_at     TIMESTAMPTZ,
    last_success_at TIMESTAMPTZ,
    cursor          TEXT,    -- source-specific: etag, last_date, file_hash, etc.
    metadata        JSONB    -- any extra state the scraper needs
);

-- Structured per-record error log. Never truncate.
CREATE TABLE IF NOT EXISTS scrape_errors (
    id      BIGSERIAL PRIMARY KEY,
    job_id  BIGINT REFERENCES scrape_jobs(id),
    level   TEXT NOT NULL CHECK (level IN ('warning','error','critical')),
    message TEXT NOT NULL,
    context JSONB,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS scrape_errors_job_idx ON scrape_errors (job_id);
