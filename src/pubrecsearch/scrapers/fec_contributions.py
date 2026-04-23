"""FEC Individual Contributions scraper.

Source: https://www.fec.gov/data/browse-data/?tab=bulk-data
Format: Pipe-delimited text in ZIP archive, no header row
Update frequency: Rolling — full-cycle file grows as filings are processed
Schedule: Quarterly (0 2 1 */3 *)
Relationship type: fec_donor

Performance note: FEC files are 300-700 MB compressed, 2-5 GB uncompressed per
cycle, with 40-80M individual contribution transactions. This scraper implements
parse_to_db() instead of parse() — the runner detects this and calls it directly,
bypassing per-row Python inserts. parse_to_db() uses Polars GROUP BY to deduplicate
to ~10-30M unique contributors per cycle, then PostgreSQL COPY + single CTE to
bulk-insert individuals + individual_documents in minutes rather than days.
"""

import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import date

import httpx
import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..db import bulk_insert_individuals, get_conn
from ..models import DownloadTarget, ParsedRecord
from ..normalize import normalize_name

_BASE_URL = "https://www.fec.gov/files/bulk-downloads"

# Pipe-delimited, no header — positional columns per FEC data dictionary
_COLS = [
    "cmte_id", "amndt_ind", "rpt_tp", "transaction_pgi", "image_num",
    "transaction_tp", "entity_tp", "name", "city", "state", "zip_code",
    "employer", "occupation", "transaction_dt", "transaction_amt",
    "other_id", "tran_id", "file_num", "memo_cd", "memo_text", "sub_id",
]

_HEADERS = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}

# Batch size for COPY inserts (controls peak RAM per batch)
_BATCH_SIZE = 500_000


def _cycle_year(today: date | None = None) -> int:
    """End year of the current 2-year FEC election cycle (always even)."""
    y = (today or date.today()).year
    return y if y % 2 == 0 else y + 1


def _cycle_url(year: int) -> str:
    yy = str(year)[2:]
    return f"{_BASE_URL}/{year}/indiv{yy}.zip"


class FecContributionsScraper(BaseScraper):
    source_id = "fec_contributions"
    schedule = "0 2 1 */3 *"   # quarterly
    doc_type = "zip"

    def discover(self, state: dict) -> list[DownloadTarget]:
        year = _cycle_year()
        period = str(year)
        url = _cycle_url(year)

        # HEAD check: skip download if file hasn't changed since last run
        try:
            with make_client(timeout=30, headers=_HEADERS) as client:
                resp = client.head(url)
                resp.raise_for_status()
                etag = resp.headers.get("etag", "")
                last_mod = resp.headers.get("last-modified", "")
                remote_sig = f"{etag}|{last_mod}"
        except Exception:
            remote_sig = ""

        stored_sig = (state.get("metadata") or {}).get("sig", "")
        if remote_sig and remote_sig == stored_sig:
            return []

        return [
            DownloadTarget(
                url=url,
                source_id=self.source_id,
                period=period,
                doc_type=self.doc_type,
                filename=f"fec_indiv_{period}.zip",
                metadata={"cursor_after": period, "sig": remote_sig},
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=10, max=120), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=600, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse_to_db(self, raw: bytes, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert FEC contributors using PostgreSQL COPY.

        Returns (records_processed, records_new) — records_processed is the
        count of unique contributors after deduplication.
        """
        tmp_dir = tempfile.mkdtemp()
        try:
            txt_path = _decompress_zip(raw, tmp_dir)
            return _bulk_ingest(txt_path, doc_id, target)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Fallback parse — not used in normal operation (parse_to_db takes priority)."""
        return []


def _decompress_zip(raw: bytes, tmp_dir: str) -> str:
    """Extract the .txt file from an FEC ZIP to a temp path."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        txt_names = [n for n in zf.namelist() if n.lower().endswith(".txt")]
        if not txt_names:
            raise ValueError(f"No .txt in FEC zip; found: {zf.namelist()}")
        out_path = os.path.join(tmp_dir, txt_names[0].split("/")[-1])
        with zf.open(txt_names[0]) as src, open(out_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return out_path


def _bulk_ingest(txt_path: str, doc_id: int, target: DownloadTarget) -> tuple[int, int]:
    """Stream file → Polars GROUP BY → bulk COPY batches → PostgreSQL."""
    # Aggregate: one row per unique (name, state, city) with contribution stats
    df = (
        pl.scan_csv(
            txt_path,
            separator="|",
            has_header=False,
            new_columns=_COLS,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
            encoding="utf8-lossy",
        )
        .filter(pl.col("entity_tp").str.strip_chars() == "IND")
        .filter(pl.col("name").is_not_null() & (pl.col("name").str.strip_chars() != ""))
        .with_columns([
            pl.col("name").str.strip_chars(),
            pl.col("state").str.strip_chars().fill_null(""),
            pl.col("city").str.strip_chars().fill_null(""),
            pl.col("employer").str.strip_chars().fill_null(""),
            pl.col("occupation").str.strip_chars().fill_null(""),
            pl.col("transaction_amt")
              .str.strip_chars()
              .cast(pl.Float64, strict=False)
              .fill_null(0.0)
              .alias("amt_f"),
        ])
        .group_by(["name", "state", "city"])
        .agg([
            pl.col("employer").first(),
            pl.col("occupation").first(),
            pl.col("amt_f").sum().alias("total_amount"),
            pl.col("amt_f").count().alias("contribution_count"),
        ])
        .collect(engine="streaming")
    )

    total_rows = len(df)
    total_inserted = 0

    # Process in batches to control memory + allow progress checkpointing
    for batch_start in range(0, total_rows, _BATCH_SIZE):
        batch = df.slice(batch_start, _BATCH_SIZE)
        rows = _build_row_dicts(batch, target.period)

        with get_conn() as conn:
            inserted, _ = bulk_insert_individuals(conn, doc_id, "fec_donor", rows)
            total_inserted += inserted

    return total_rows, total_inserted


def _build_row_dicts(df: "pl.DataFrame", period: str) -> list[dict]:
    """Convert a Polars DataFrame slice into the row dicts bulk_insert_individuals expects."""
    rows = []
    for row in df.iter_rows(named=True):
        name = row["name"] or ""
        if not name:
            continue

        state = row["state"] or ""
        city = row["city"] or ""
        employer = row["employer"] or ""
        occupation = row["occupation"] or ""
        total = row["total_amount"] or 0.0
        count = row["contribution_count"] or 0

        name_norm = normalize_name(name)

        identifiers = {}
        if state:
            identifiers["state"] = state
        if employer:
            identifiers["employer"] = employer
        if occupation:
            identifiers["occupation"] = occupation
        identifiers["contribution_count"] = count
        identifiers["total_amount"] = round(total, 2)
        identifiers["cycle"] = period

        excerpt_parts = [f"FEC donor {period}"]
        if city and state:
            excerpt_parts.append(f"{city}, {state}")
        if count > 1:
            excerpt_parts.append(f"{count} contributions totaling ${total:,.0f}")
        else:
            excerpt_parts.append(f"${total:,.0f}")

        rows.append({
            "name": name,
            "name_norm": name_norm,
            "excerpt": " | ".join(excerpt_parts),
            "identifiers": identifiers,
        })

    return rows
