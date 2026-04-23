"""PostgreSQL connection and write helpers.

Uses psycopg2 throughout — synchronous, which is appropriate for a batch
scraping pipeline where COPY performance matters more than concurrency.
"""

import hashlib
import io
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from psycopg2.extensions import connection as PGConnection

from .config import get_settings


@contextmanager
def get_conn() -> Generator[PGConnection, None, None]:
    """Yield a psycopg2 connection; commit on clean exit, rollback on error."""
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scrape job management
# ---------------------------------------------------------------------------


def start_job(source_id: str) -> int:
    """Insert a running scrape_jobs row and return its id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_jobs (source_id, status)
                VALUES (%s, 'running')
                RETURNING id
                """,
                (source_id,),
            )
            return cur.fetchone()[0]


def finish_job(
    job_id: int,
    status: str,
    records_processed: int = 0,
    records_new: int = 0,
    errors_count: int = 0,
    notes: str | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE scrape_jobs
                SET finished_at = now(), status = %s,
                    records_processed = %s, records_new = %s,
                    errors_count = %s, notes = %s
                WHERE id = %s
                """,
                (status, records_processed, records_new, errors_count, notes, job_id),
            )


def log_error(
    job_id: int, level: str, message: str, context: dict[str, Any] | None = None
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_errors (job_id, level, message, context)
                VALUES (%s, %s, %s, %s)
                """,
                (job_id, level, message, json.dumps(context) if context else None),
            )


# ---------------------------------------------------------------------------
# Scrape state (incremental cursor)
# ---------------------------------------------------------------------------


def get_state(source_id: str) -> dict[str, Any]:
    """Return the scrape_state row for a source as a plain dict."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM scrape_state WHERE source_id = %s", (source_id,))
            row = cur.fetchone()
            return dict(row) if row else {}


def set_state(
    source_id: str,
    cursor: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO scrape_state (source_id, last_run_at, last_success_at, cursor, metadata)
                VALUES (%s, now(), now(), %s, %s)
                ON CONFLICT (source_id) DO UPDATE SET
                    last_run_at = now(),
                    last_success_at = now(),
                    cursor = EXCLUDED.cursor,
                    metadata = EXCLUDED.metadata
                """,
                (source_id, cursor, json.dumps(metadata) if metadata else None),
            )


# ---------------------------------------------------------------------------
# Document deduplication
# ---------------------------------------------------------------------------


def document_exists(file_hash: str) -> bool:
    """Return True if a document with this SHA-256 hash is already stored."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM documents WHERE file_hash = %s LIMIT 1", (file_hash,))
            return cur.fetchone() is not None


def insert_document(
    r2_key: str,
    source_id: str,
    source_url: str | None,
    file_hash: str,
    file_size: int,
    doc_type: str,
    period: str,
) -> int:
    """Insert a document row and return its id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents
                    (r2_key, source_id, source_url, captured_at, file_hash, file_size, doc_type, period)
                VALUES (%s, %s, %s, now(), %s, %s, %s, %s)
                RETURNING id
                """,
                (r2_key, source_id, source_url, file_hash, file_size, doc_type, period),
            )
            return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Individual insertion (Option A — no dedup across sources)
# ---------------------------------------------------------------------------


def insert_individual(name: str, name_norm: str, aliases: list[str] | None = None) -> int:
    """Insert a new individual row and return its id."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO individuals (name, name_norm, aliases)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (name, name_norm, json.dumps(aliases) if aliases else None),
            )
            return cur.fetchone()[0]


def insert_individual_document(
    individual_id: int,
    document_id: int,
    relationship: str,
    excerpt: str | None = None,
    identifiers: dict[str, Any] | None = None,
) -> None:
    """Insert or ignore an individual_documents linkage row."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO individual_documents
                    (individual_id, document_id, relationship, excerpt, identifiers)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    individual_id,
                    document_id,
                    relationship,
                    excerpt,
                    json.dumps(identifiers) if identifiers else None,
                ),
            )


# ---------------------------------------------------------------------------
# Bulk COPY helpers (Phase 2 large sources: FEC, EDGAR, CMS, etc.)
# ---------------------------------------------------------------------------


def bulk_insert_individuals(
    conn: PGConnection,
    doc_id: int,
    relationship: str,
    rows: list[dict],
) -> tuple[int, int]:
    """COPY a batch of contributor rows, insert individuals + linkages in bulk.

    Each row dict must have: name, name_norm, excerpt, identifiers (dict).
    Uses a staging temp table + single CTE to insert individuals and
    individual_documents in one round-trip. Returns (inserted, total).

    This is 100-1000x faster than per-row inserts for millions of records.
    """
    if not rows:
        return 0, 0

    # Build TSV buffer: name \\t name_norm \\t excerpt \\t identifiers_json
    buf = io.StringIO()
    for r in rows:
        buf.write(
            f"{_pg_escape(r['name'])}\t"
            f"{_pg_escape(r['name_norm'])}\t"
            f"{_pg_escape(r.get('excerpt', ''))}\t"
            f"{_pg_escape(json.dumps(r.get('identifiers') or {}))}\n"
        )
    buf.seek(0)

    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TEMP TABLE _bulk_staging (
                name TEXT, name_norm TEXT, excerpt TEXT, identifiers TEXT
            ) ON COMMIT DROP
            """
        )
        cur.copy_expert("COPY _bulk_staging FROM STDIN", buf)

        # Single CTE: insert distinct individuals, then link them to doc
        cur.execute(
            """
            WITH new_individuals AS (
                INSERT INTO individuals (name, name_norm)
                SELECT DISTINCT ON (name_norm) name, name_norm
                FROM _bulk_staging
                ORDER BY name_norm
                RETURNING id, name_norm
            )
            INSERT INTO individual_documents
                (individual_id, document_id, relationship, excerpt, identifiers)
            SELECT
                ni.id,
                %(doc_id)s,
                %(rel)s,
                s.excerpt,
                s.identifiers::jsonb
            FROM new_individuals ni
            JOIN (
                SELECT DISTINCT ON (name_norm) name_norm, excerpt, identifiers
                FROM _bulk_staging
                ORDER BY name_norm
            ) s ON s.name_norm = ni.name_norm
            ON CONFLICT DO NOTHING
            """,
            {"doc_id": doc_id, "rel": relationship},
        )
        inserted = cur.rowcount

    return inserted, len(rows)


def bulk_copy_individuals(
    conn: PGConnection,
    rows: list[tuple[str, str, str | None]],
) -> list[int]:
    """COPY a batch of (name, name_norm, aliases_json) into individuals.

    Returns the list of inserted ids in the same order.
    Kept for backward compatibility; prefer bulk_insert_individuals() for
    new sources where doc linkage is also needed.
    """
    buf = io.StringIO()
    for name, name_norm, aliases_json in rows:
        buf.write(f"{_pg_escape(name)}\t{_pg_escape(name_norm)}\t{_pg_escape(aliases_json or '')}\n")
    buf.seek(0)

    with conn.cursor() as cur:
        tmp = "tmp_ind_copy"
        cur.execute(
            f"CREATE TEMP TABLE {tmp} (name TEXT, name_norm TEXT, aliases TEXT) ON COMMIT DROP"
        )
        cur.copy_expert(f"COPY {tmp} FROM STDIN", buf)
        cur.execute(
            f"""
            INSERT INTO individuals (name, name_norm, aliases)
            SELECT name, name_norm, aliases::jsonb
            FROM {tmp}
            RETURNING id
            """
        )
        return [r[0] for r in cur.fetchall()]


def _pg_escape(s: str) -> str:
    """Escape a string for PostgreSQL tab-separated COPY format."""
    if s is None or s == "":
        return r"\N"
    return s.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
