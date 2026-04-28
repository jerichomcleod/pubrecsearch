# Query API

The query API is a FastAPI application (`src/pubrecsearch/api/main.py`) that exposes the contents of the PostgreSQL database over HTTP. It is intended for local use only — there is no authentication, rate limiting, or multi-tenancy. Start it with:

```bash
pubrecsearch serve [--host 127.0.0.1] [--port 8000] [--reload]
```

Default: `http://127.0.0.1:8000`. Interactive documentation (Swagger UI) is automatically available at `http://127.0.0.1:8000/docs`.

---

## CORS

The API adds `CORSMiddleware` that allows:
- `allow_origins=["*"]` — all origins
- `allow_methods=["GET"]` — read-only
- `allow_headers=["*"]`

This is intentional: the API is local and read-only, so open CORS is harmless and simplifies use from browser-based tools or notebooks.

---

## Endpoints

### `GET /health`

Verifies the database connection is alive.

**Response (200):**
```json
{"status": "ok"}
```

**Response (503):** If the database is unreachable, returns `{"detail": "<error message>"}`.

**Assumption:** A successful `SELECT 1` against PostgreSQL is sufficient to confirm the system is operational. The API does not check R2 connectivity or scraper health.

---

### `GET /sources`

Returns the most recent scrape job status for every source that has ever run. Uses `DISTINCT ON (source_id) ... ORDER BY source_id, started_at DESC` to get one row per source.

**Response (200):**
```json
{
  "sources": [
    {
      "source_id": "ofac_sdn",
      "status": "success",
      "started_at": "2025-04-27T07:00:01.234Z",
      "finished_at": "2025-04-27T07:00:45.678Z",
      "records_processed": 14823,
      "records_new": 0,
      "errors_count": 0,
      "notes": null
    },
    ...
  ]
}
```

**Notes:**
- Sources that have never run do not appear in the response — only sources with at least one `scrape_jobs` row are listed.
- `records_new: 0` with `status: "success"` is the normal state for a full-replacement source (OFAC, FARA, ATF FFL) when the file hash hasn't changed since the last run.

---

### `GET /dashboard`

Returns an HTML page with a human-readable scrape health dashboard. Auto-refreshes every 5 minutes via `<meta http-equiv="refresh" content="300">`.

**Response (200):** `Content-Type: text/html`

The dashboard shows:
- **Summary stats** at the top: total individuals indexed, total documents stored, error count in the last 24 hours (pulled from `scrape_errors WHERE ts > now() - interval '24 hours'`)
- **Per-source table** with columns: Source, Status, New Records, Errors, Last Run, Cursor

Status colors:
- `success` → green (`#2e7d32`)
- `partial` → orange (`#e65100`)
- `failed` → red (`#c62828`)
- `running` → blue (`#1565c0`)

The dashboard query joins `scrape_jobs` with `scrape_state` to show both the last job outcome and the current cursor value. The cursor is truncated to 30 characters in the display.

**Assumption:** The dashboard is accessed from a browser on the same machine. No authentication is needed.

---

### `GET /search`

Fuzzy name search using PostgreSQL trigram similarity.

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `q` | string | Yes | — | Name to search for (min 2 characters) |
| `source` | string | No | null | Filter results to a single `source_id` |
| `limit` | integer | No | 20 | Max results to return (1–100) |
| `offset` | integer | No | 0 | Pagination offset |

**How search works:**

1. The query string `q` is passed through `normalize_name()` — the same normalization applied at ingest. This means `"JOHN A. SMITH"` and `"john a. smith"` and `"John Smith"` all produce equivalent searches.

2. If `normalize_name(q)` returns an empty string (e.g., `q = "..."` or `q = " "`), a 400 is returned.

3. The normalized query is compared against `individuals.name_norm` using the `%` operator from `pg_trgm`. This operator returns `true` if `similarity(name_norm, q_norm) > pg_trgm.similarity_threshold` (default threshold: 0.3).

4. Results are scored with `similarity(name_norm, q_norm)` and sorted by score descending, then alphabetically by name for ties.

5. Each result row is aggregated: `array_agg(DISTINCT source_id)` collects all sources the individual appears in, and `count(DISTINCT document_id)` counts their total document count.

**The trigram similarity threshold** of 0.3 is PostgreSQL's default for the `%` operator. It is not configurable via the API. To change it globally: `SET pg_trgm.similarity_threshold = 0.4;` before queries, or permanently in `postgresql.conf`.

**Response (200):**
```json
{
  "query": "John Smith",
  "results": [
    {
      "id": 12345,
      "name": "JOHN A. SMITH",
      "name_norm": "john a smith",
      "aliases": null,
      "score": 0.857,
      "sources": ["fec_contributions", "ofac_sdn"],
      "document_count": 3
    },
    {
      "id": 67890,
      "name": "John Smith Jr.",
      "name_norm": "john smith jr",
      "aliases": null,
      "score": 0.714,
      "sources": ["lda"],
      "document_count": 1
    }
  ],
  "count": 2
}
```

**Response fields:**

- `id`: The `individuals.id`. Use this to fetch the full individual record.
- `name`: Original name as extracted from the source.
- `name_norm`: The normalized form stored in the DB (included for debugging).
- `aliases`: JSON array of alternate name spellings, or null. Only populated for OFAC SDN records.
- `score`: Trigram similarity score between `name_norm` and the normalized query (0.0–1.0).
- `sources`: Array of distinct `source_id` strings for all documents this individual appears in.
- `document_count`: Total number of documents linked to this individual.
- `count`: The number of results returned in this response (not the total matching count).

**Assumption:** The search does not attempt to count the total number of matching rows — only the rows returned in the current page are counted. There is no `total_count` field. The caller can determine if there are more results by checking whether `count == limit`.

**Performance:** The trigram GIN index (`individuals_trgm_idx`) makes the `%` operator fast even across millions of rows. Without the index, this query degrades to a full sequential scan. The index must exist for acceptable performance.

---

### `GET /individual/{id}`

Returns full details for one individual, including all linked documents inline.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | integer | The `individuals.id` |

**Response (200):**
```json
{
  "id": 12345,
  "name": "JOHN A. SMITH",
  "name_norm": "john a smith",
  "aliases": null,
  "merge_group_id": null,
  "created_at": "2025-01-15T12:34:56.789Z",
  "updated_at": "2025-01-15T12:34:56.789Z",
  "documents": [
    {
      "relationship": "fec_donor",
      "excerpt": "Donor | $2,500 | 2024-03-15 | Acme Corp | engineer",
      "identifiers": {
        "state": "CA",
        "employer": "Acme Corp",
        "total_amount": "2500.00",
        "contribution_count": "1"
      },
      "document_id": 456,
      "source_id": "fec_contributions",
      "period": "2024",
      "captured_at": "2025-01-15T12:34:56.789Z",
      "doc_type": "zip"
    }
  ]
}
```

**Response (404):** `{"detail": "Individual not found"}`

**Notes:**
- The `documents` array is sorted by `captured_at DESC` (most recently ingested documents first).
- An individual can have multiple entries in `documents` if they appear in multiple source files or in multiple roles within the same file.
- `identifiers` is a JSONB object; its fields vary by source (see database.md for details).

---

### `GET /individual/{id}/documents`

Returns the linked documents for one individual, with pagination and optional source filtering. Useful when an individual has many linked documents (e.g., a frequent campaign donor).

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | integer | The `individuals.id` |

**Query parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `source` | string | No | null | Filter to documents from one `source_id` |
| `limit` | integer | No | 50 | Max results (1–200) |
| `offset` | integer | No | 0 | Pagination offset |

**Response (200):**
```json
{
  "documents": [
    {
      "relationship": "fec_donor",
      "excerpt": "...",
      "identifiers": {...},
      "document_id": 456,
      "source_id": "fec_contributions",
      "period": "2024",
      "captured_at": "2025-01-15T12:34:56.789Z",
      "doc_type": "zip"
    }
  ]
}
```

**Note:** This endpoint does not return 404 if the individual doesn't exist — it returns an empty `documents` array. This is a minor inconsistency with `GET /individual/{id}`. The caller should use `/individual/{id}` first to confirm the individual exists.

---

### `GET /document/{id}`

Returns metadata for one raw source document, plus a time-limited presigned download URL for the R2-archived file.

**Path parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `id` | integer | The `documents.id` |

**Response (200):**
```json
{
  "id": 456,
  "r2_key": "raw/fec_contributions/2024/fec_indiv_2024.zip",
  "source_id": "fec_contributions",
  "source_url": "https://cg-519a459a-0ea3-42c2-b7bc-fa1143481f74.s3-us-gov-west-1.amazonaws.com/...",
  "captured_at": "2025-01-15T12:34:56.789Z",
  "file_hash": "a1b2c3d4...",
  "file_size": 312456789,
  "doc_type": "zip",
  "period": "2024",
  "download_url": "https://pubrecsearch-raw.r2.cloudflarestorage.com/raw/fec_contributions/2024/fec_indiv_2024.zip?X-Amz-Expires=3600&..."
}
```

**Response (404):** `{"detail": "Document not found"}`

**`download_url`:** A presigned R2 URL valid for **3,600 seconds (1 hour)** from the time of the API call. Generated by `r2.presigned_url(r2_key, expires_in=3600)`. The URL is not stored in the database — it is generated on every request.

**Assumption:** The R2 credentials in `.env` have `GetObject` permission on the bucket. If credentials are missing or invalid, `presigned_url()` will raise an exception and the endpoint will return a 500.

---

## Serialization

All responses are serialized by the `_serialize()` helper, which converts:
- `datetime` objects → ISO 8601 strings (`datetime.isoformat()`)
- `memoryview` objects → decoded UTF-8 strings (used for PostgreSQL `bytea` fields)
- All other values → passed through unchanged

PostgreSQL `JSONB` columns are automatically deserialized to Python dicts/lists by psycopg2's `RealDictCursor` and are returned as-is in JSON responses.

---

## Error Responses

FastAPI returns errors in the format:
```json
{"detail": "error message"}
```

| Status | Condition |
|--------|-----------|
| 400 | Search query normalizes to empty string |
| 404 | Individual or document ID not found |
| 422 | Query parameter validation failure (e.g., `limit` out of range) |
| 500 | Unhandled exception (database error, R2 error, etc.) |
| 503 | Database unreachable (health endpoint only) |

---

## Pagination Notes

- `/search`: `limit` max 100, offset-based. No cursor pagination.
- `/individual/{id}/documents`: `limit` max 200.
- There is no `total_count` in any paginated response. To determine if there is a next page, check whether the returned `count` equals `limit` (for `/search`) or whether the returned `documents` array length equals `limit` (for `/individual/{id}/documents`).

---

## Running with Reload

```bash
pubrecsearch serve --reload
```

`--reload` passes `reload=True` to uvicorn, which watches source files and restarts the server on changes. Use only during development — it adds overhead and should not be used in production.
