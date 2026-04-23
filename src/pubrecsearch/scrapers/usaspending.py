"""USASpending.gov individual recipient scraper.

Source: https://api.usaspending.gov/
Format: Bulk award download API (async job → ZIP → CSV)
Update frequency: Weekly
Schedule: Weekly (0 4 * * 2)
Relationship type: usaspending_recipient

Filters to awards where recipient_type includes "Individual", which covers
grants, contracts, and loans made directly to named individuals.

The USASpending bulk download API is async: POST creates a job, fetch()
polls for completion, then downloads the resulting ZIP. This differs from
the standard BaseScraper pattern; discover() creates a synthetic target
with the job parameters encoded in metadata, and fetch() handles polling.

Historical backfill: scripts/bootstrap/usaspending_bootstrap.py
"""

import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from datetime import date, timedelta

import httpx
import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..db import bulk_insert_individuals, get_conn
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

_BULK_API = "https://api.usaspending.gov/api/v2/bulk_download/awards/"
_STATUS_API = "https://api.usaspending.gov/api/v2/bulk_download/status/"
_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "PubRecSearch/1.0 (research@example.com)",
}

# Columns we extract from USASpending award CSVs
_NAME_CANDIDATES = [
    "recipient_name",
    "prime_award_recipient_name",
    "awardee_or_recipient_legal_business_name",
]
_AMOUNT_CANDIDATES = ["total_obligated_amount", "federal_action_obligation", "award_amount"]
_DATE_CANDIDATES = ["period_of_performance_start_date", "action_date", "start_date"]
_ID_CANDIDATES = ["award_id_piid", "fain", "uri", "award_id"]
_TYPE_CANDIDATES = ["award_type", "assistance_type"]
_STATE_CANDIDATES = ["recipient_state_code", "place_of_performance_state_code"]


class UsaspendingScraper(BaseScraper):
    source_id = "usaspending"
    schedule = "0 4 * * 2"   # weekly on Tuesday
    doc_type = "zip"

    def discover(self, state: dict) -> list[DownloadTarget]:
        last_cursor = state.get("cursor")
        today = date.today()

        if last_cursor:
            start_date = last_cursor
        else:
            start_date = (today - timedelta(days=7)).isoformat()

        end_date = today.isoformat()
        period = today.isoformat()

        return [
            DownloadTarget(
                url=_BULK_API,
                source_id=self.source_id,
                period=period,
                doc_type=self.doc_type,
                filename=f"usaspending_{period}.zip",
                metadata={
                    "cursor_after": period,
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )
        ]

    def fetch(self, target: DownloadTarget) -> bytes:
        """Create bulk download job, poll to completion, return ZIP bytes."""
        start_date = target.metadata.get("start_date")
        end_date = target.metadata.get("end_date")

        payload = {
            "filters": {
                "recipient_type_names": ["Individual"],
                "time_period": [{"start_date": start_date, "end_date": end_date}],
            },
            "award_levels": ["prime_awards"],
            "columns": [],
        }

        with make_client(timeout=60, headers=_HEADERS) as client:
            resp = client.post(_BULK_API, json=payload)
            resp.raise_for_status()
            job = resp.json()

        file_name = job.get("file_name", "")
        if not file_name:
            raise RuntimeError(f"No file_name in USASpending response: {job}")

        # Poll for job completion (max ~10 minutes)
        download_url = self._poll_job(file_name)

        with make_client(timeout=300) as client:
            resp = client.get(download_url)
            resp.raise_for_status()
            return resp.content

    def _poll_job(self, file_name: str, max_wait: int = 600) -> str:
        """Poll the status endpoint until the download is ready."""
        deadline = time.time() + max_wait
        with make_client(timeout=30, headers=_HEADERS) as client:
            while time.time() < deadline:
                resp = client.get(_STATUS_API, params={"file_name": file_name})
                resp.raise_for_status()
                status = resp.json()

                if status.get("status") == "finished":
                    return status["file_url"]
                if status.get("status") == "failed":
                    raise RuntimeError(f"USASpending job failed: {status.get('message')}")

                time.sleep(15)

        raise TimeoutError(f"USASpending download job timed out after {max_wait}s")

    def parse_to_db(self, raw: bytes, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert USASpending individual recipients using PostgreSQL COPY."""
        tmp_dir = tempfile.mkdtemp()
        try:
            csv_paths = _extract_csvs(raw, tmp_dir)
            all_rows = []
            for path in csv_paths:
                records = _parse_usaspending_csv(path, target)
                for rec in records:
                    p = rec.individuals[0]
                    all_rows.append({
                        "name": p.name,
                        "name_norm": normalize_name(p.name),
                        "excerpt": p.excerpt or "",
                        "identifiers": p.identifiers or {},
                    })
            if not all_rows:
                return 0, 0
            with get_conn() as conn:
                inserted, total = bulk_insert_individuals(conn, doc_id, "usaspending_recipient", all_rows)
            return total, inserted
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Fallback parse — not used (parse_to_db takes priority)."""
        return []


def _extract_csvs(raw: bytes, tmp_dir: str) -> list[str]:
    """Extract all CSVs from the USASpending download ZIP."""
    paths = []
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".csv"):
                    out = os.path.join(tmp_dir, os.path.basename(name))
                    with zf.open(name) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    paths.append(out)
    except zipfile.BadZipFile:
        # Not a ZIP — probably a direct CSV
        out = os.path.join(tmp_dir, "data.csv")
        with open(out, "wb") as f:
            f.write(raw)
        paths.append(out)
    return paths


def _parse_usaspending_csv(path: str, target: DownloadTarget) -> list[ParsedRecord]:
    """Stream a USASpending CSV with Polars, extract individual recipients."""
    header_df = pl.read_csv(path, n_rows=0, infer_schema_length=0)
    available = {c.lower(): c for c in header_df.columns}

    name_col = next((available[c] for c in _NAME_CANDIDATES if c in available), None)
    if not name_col:
        return []

    amount_col = next((available[c] for c in _AMOUNT_CANDIDATES if c in available), None)
    date_col = next((available[c] for c in _DATE_CANDIDATES if c in available), None)
    id_col = next((available[c] for c in _ID_CANDIDATES if c in available), None)
    type_col = next((available[c] for c in _TYPE_CANDIDATES if c in available), None)
    state_col = next((available[c] for c in _STATE_CANDIDATES if c in available), None)

    select_cols = [c for c in [name_col, amount_col, date_col, id_col, type_col, state_col] if c]

    df = (
        pl.scan_csv(
            path,
            has_header=True,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
            encoding="utf8-lossy",
        )
        .filter(pl.col(name_col).is_not_null() & (pl.col(name_col) != ""))
        .select(select_cols)
        .collect(engine="streaming")
    )

    records: list[ParsedRecord] = []
    for row in df.iter_rows(named=True):
        name = (row.get(name_col) or "").strip()
        if not name:
            continue

        award_id = (row.get(id_col) or "").strip() if id_col else ""
        amount = (row.get(amount_col) or "").strip() if amount_col else ""
        award_date = (row.get(date_col) or "").strip() if date_col else ""
        award_type = (row.get(type_col) or "").strip() if type_col else ""
        state = (row.get(state_col) or "").strip() if state_col else ""

        identifiers: dict = {}
        if award_id:
            identifiers["award_id"] = award_id
        if award_type:
            identifiers["award_type"] = award_type
        if state:
            identifiers["state"] = state

        excerpt_parts = ["USASpending award"]
        if award_type:
            excerpt_parts.append(award_type)
        if amount:
            try:
                excerpt_parts.append(f"${float(amount):,.0f}")
            except (ValueError, TypeError):
                pass
        if award_date:
            excerpt_parts.append(award_date[:10])

        records.append(
            ParsedRecord(
                individuals=[
                    ParsedIndividual(
                        name=name,
                        relationship="usaspending_recipient",
                        excerpt=" | ".join(excerpt_parts),
                        identifiers=identifiers,
                    )
                ],
                source_identifier=award_id or name,
            )
        )

    return records
