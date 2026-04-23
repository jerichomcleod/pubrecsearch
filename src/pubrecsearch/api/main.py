"""FastAPI query API.

Endpoints:
  GET /search?q=&source=&limit=&offset=
  GET /individual/{id}
  GET /individual/{id}/documents
  GET /document/{id}
  GET /sources
  GET /health
  GET /dashboard          — HTML scrape health dashboard
"""

import json
from datetime import datetime, timezone
from typing import Any

import psycopg2.extras
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from ..config import get_settings
from ..db import get_conn
from ..normalize import normalize_name
from ..r2 import presigned_url

app = FastAPI(title="PubRecSearch", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Sources — scrape job status
# ---------------------------------------------------------------------------


@app.get("/sources")
def sources():
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (source_id)
                    source_id,
                    status,
                    started_at,
                    finished_at,
                    records_processed,
                    records_new,
                    errors_count,
                    notes
                FROM scrape_jobs
                ORDER BY source_id, started_at DESC
                """
            )
            return {"sources": [dict(r) for r in cur.fetchall()]}


# ---------------------------------------------------------------------------
# Scrape health dashboard
# ---------------------------------------------------------------------------


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """HTML scrape health dashboard — shows last job status per source."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (j.source_id)
                    j.source_id,
                    j.status,
                    j.started_at,
                    j.finished_at,
                    j.records_processed,
                    j.records_new,
                    j.errors_count,
                    j.notes,
                    s.cursor,
                    s.last_success_at
                FROM scrape_jobs j
                LEFT JOIN scrape_state s ON s.source_id = j.source_id
                ORDER BY j.source_id, j.started_at DESC
                """
            )
            jobs = [_serialize(dict(r)) for r in cur.fetchall()]

            cur.execute("SELECT COUNT(*) FROM individuals")
            total_individuals = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) FROM documents")
            total_documents = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) FROM scrape_errors
                WHERE ts > now() - interval '24 hours'
                """
            )
            recent_errors = cur.fetchone()["count"]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = ""
    for j in sorted(jobs, key=lambda x: x["source_id"]):
        status = j["status"]
        status_color = {"success": "#2e7d32", "partial": "#e65100", "failed": "#c62828",
                        "running": "#1565c0"}.get(status, "#555")
        finished = j.get("finished_at", "—") or "—"
        if finished != "—" and "T" in str(finished):
            finished = str(finished)[:16].replace("T", " ")
        cursor = j.get("cursor") or "—"
        rows += f"""
        <tr>
          <td><code>{j['source_id']}</code></td>
          <td style="color:{status_color};font-weight:bold">{status}</td>
          <td>{j.get('records_new', 0):,}</td>
          <td>{j.get('errors_count', 0)}</td>
          <td>{finished}</td>
          <td style="font-size:0.85em;color:#666">{cursor[:30]}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
  <title>PubRecSearch — Scrape Dashboard</title>
  <meta http-equiv="refresh" content="300">
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .stats {{ display: flex; gap: 2rem; margin: 1rem 0 1.5rem; }}
    .stat {{ background: #f5f5f5; border-radius: 8px; padding: 0.75rem 1.25rem; }}
    .stat-num {{ font-size: 1.8rem; font-weight: bold; color: #1565c0; }}
    .stat-label {{ font-size: 0.85rem; color: #666; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th {{ text-align: left; padding: 0.5rem 1rem; background: #e8e8e8;
           border-bottom: 2px solid #ccc; font-size: 0.85rem; text-transform: uppercase; }}
    td {{ padding: 0.5rem 1rem; border-bottom: 1px solid #eee; }}
    tr:hover td {{ background: #fafafa; }}
    .ts {{ font-size: 0.8rem; color: #999; margin-top: 2rem; }}
  </style>
</head>
<body>
  <h1>PubRecSearch Scrape Dashboard</h1>
  <div class="stats">
    <div class="stat">
      <div class="stat-num">{total_individuals:,}</div>
      <div class="stat-label">Individuals indexed</div>
    </div>
    <div class="stat">
      <div class="stat-num">{total_documents:,}</div>
      <div class="stat-label">Documents stored</div>
    </div>
    <div class="stat">
      <div class="stat-num" style="color:{'#c62828' if recent_errors > 0 else '#2e7d32'}">{recent_errors}</div>
      <div class="stat-label">Errors (24h)</div>
    </div>
  </div>
  <table>
    <thead>
      <tr>
        <th>Source</th><th>Status</th><th>New Records</th>
        <th>Errors</th><th>Last Run</th><th>Cursor</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>
  <p class="ts">Last refreshed: {now} &nbsp;·&nbsp; Auto-refreshes every 5 minutes</p>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@app.get("/search")
def search(
    q: str = Query(..., min_length=2, description="Name to search"),
    source: str | None = Query(None, description="Filter by source_id"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    q_norm = normalize_name(q)
    if not q_norm:
        raise HTTPException(status_code=400, detail="Query normalizes to empty string")

    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Trigram similarity search on normalized name
            if source:
                cur.execute(
                    """
                    SELECT
                        i.id,
                        i.name,
                        i.name_norm,
                        i.aliases,
                        similarity(i.name_norm, %s) AS score,
                        array_agg(DISTINCT id_doc.source_id) AS sources,
                        count(DISTINCT id_doc.document_id) AS document_count
                    FROM individuals i
                    JOIN individual_documents id_link ON id_link.individual_id = i.id
                    JOIN documents id_doc ON id_doc.id = id_link.document_id
                    WHERE i.name_norm %% %s
                      AND id_doc.source_id = %s
                    GROUP BY i.id, i.name, i.name_norm, i.aliases
                    ORDER BY score DESC, i.name
                    LIMIT %s OFFSET %s
                    """,
                    (q_norm, q_norm, source, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        i.id,
                        i.name,
                        i.name_norm,
                        i.aliases,
                        similarity(i.name_norm, %s) AS score,
                        array_agg(DISTINCT id_doc.source_id) AS sources,
                        count(DISTINCT id_doc.document_id) AS document_count
                    FROM individuals i
                    JOIN individual_documents id_link ON id_link.individual_id = i.id
                    JOIN documents id_doc ON id_doc.id = id_link.document_id
                    WHERE i.name_norm %% %s
                    GROUP BY i.id, i.name, i.name_norm, i.aliases
                    ORDER BY score DESC, i.name
                    LIMIT %s OFFSET %s
                    """,
                    (q_norm, q_norm, limit, offset),
                )

            results = [_serialize(dict(r)) for r in cur.fetchall()]
            return {"query": q, "results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Individual
# ---------------------------------------------------------------------------


@app.get("/individual/{individual_id}")
def get_individual(individual_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM individuals WHERE id = %s",
                (individual_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Individual not found")

            # Fetch linked documents with relationship context
            cur.execute(
                """
                SELECT
                    id_link.relationship,
                    id_link.excerpt,
                    id_link.identifiers,
                    d.id AS document_id,
                    d.source_id,
                    d.period,
                    d.captured_at,
                    d.doc_type
                FROM individual_documents id_link
                JOIN documents d ON d.id = id_link.document_id
                WHERE id_link.individual_id = %s
                ORDER BY d.captured_at DESC
                """,
                (individual_id,),
            )
            docs = [_serialize(dict(r)) for r in cur.fetchall()]

            individual = _serialize(dict(row))
            individual["documents"] = docs
            return individual


@app.get("/individual/{individual_id}/documents")
def get_individual_documents(
    individual_id: int,
    source: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if source:
                cur.execute(
                    """
                    SELECT
                        id_link.relationship,
                        id_link.excerpt,
                        id_link.identifiers,
                        d.id AS document_id,
                        d.source_id,
                        d.period,
                        d.captured_at,
                        d.doc_type
                    FROM individual_documents id_link
                    JOIN documents d ON d.id = id_link.document_id
                    WHERE id_link.individual_id = %s AND d.source_id = %s
                    ORDER BY d.captured_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (individual_id, source, limit, offset),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        id_link.relationship,
                        id_link.excerpt,
                        id_link.identifiers,
                        d.id AS document_id,
                        d.source_id,
                        d.period,
                        d.captured_at,
                        d.doc_type
                    FROM individual_documents id_link
                    JOIN documents d ON d.id = id_link.document_id
                    WHERE id_link.individual_id = %s
                    ORDER BY d.captured_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (individual_id, limit, offset),
                )
            return {"documents": [_serialize(dict(r)) for r in cur.fetchall()]}


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@app.get("/document/{document_id}")
def get_document(document_id: int):
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM documents WHERE id = %s", (document_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Document not found")
            doc = _serialize(dict(row))
            doc["download_url"] = presigned_url(doc["r2_key"], expires_in=3600)
            return doc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialize(d: dict[str, Any]) -> dict[str, Any]:
    """Convert non-JSON-serializable values (datetime, Decimal) to strings."""
    out = {}
    for k, v in d.items():
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        elif isinstance(v, memoryview):
            out[k] = bytes(v).decode()
        else:
            out[k] = v
    return out
