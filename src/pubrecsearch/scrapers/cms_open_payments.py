"""CMS Open Payments scraper.

Source: https://openpaymentsdata.cms.gov/
Format: Annual program-year CSV bulk downloads via CMS data portal
Update frequency: Annual (new program year published ~June each year)
Schedule: Monthly (0 5 15 * *) — detect new program year files
Relationship type: cms_payment_recipient

CMS Open Payments tracks payments from drug/device manufacturers to physicians
and teaching hospitals. We extract named physician recipients only.

discover() queries the CMS data portal metadata API to find the latest
program year dataset, then returns a DownloadTarget for the bulk CSV.

Note: CMS Open Payments files are ~2-4 GB uncompressed. parse() uses
Polars lazy streaming via a temp file.
"""

import io
import json
import os
import shutil
import tempfile
import zipfile
from datetime import date
from pathlib import Path

import httpx
import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..db import bulk_insert_individuals, get_conn
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

# CMS data portal API (Socrata-based)
_METADATA_URL = "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items"
_HEADERS = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}

# Columns in CMS General Payments CSV that identify physicians
_PHYSICIAN_COLS = [
    "Covered_Recipient_First_Name",
    "Covered_Recipient_Last_Name",
    "Covered_Recipient_Middle_Name",
    "Covered_Recipient_Profile_ID",
    "Covered_Recipient_NPI",
    "Covered_Recipient_Specialty",
    "Recipient_State",
    "Recipient_City",
    "Total_Amount_of_Payment_USDollars",
    "Date_of_Payment",
    "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
    "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
]


def _find_general_payments_url(datasets: list[dict]) -> tuple[str, str] | None:
    """Scan dataset list metadata to find the latest General Payments dataset.

    The CMS list endpoint omits distribution URLs; we must fetch each dataset
    record individually by identifier to get the real downloadURL.
    """
    # Collect (year, identifier) for all "General Payment Data" entries
    candidates = []
    for ds in datasets:
        title = ds.get("title") or ""
        if "general payment" not in title.lower():
            continue
        for word in title.split():
            if word.isdigit() and 2013 <= int(word) <= date.today().year:
                identifier = ds.get("identifier")
                if identifier:
                    candidates.append((int(word), identifier))
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)

    # Fetch the full record for the most recent year to get the download URL
    _DETAIL_URL = "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items/{identifier}"
    headers = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}
    for year, identifier in candidates:
        try:
            with make_client(timeout=30, headers=headers) as client:
                resp = client.get(_DETAIL_URL.format(identifier=identifier))
                resp.raise_for_status()
                record = resp.json()
            for dist in record.get("distribution") or []:
                dl_url = dist.get("downloadURL") or dist.get("accessURL")
                media = (dist.get("mediaType") or "").lower()
                title_d = (dist.get("title") or "").lower()
                # Prefer the "detailed dataset" general payment CSV
                if dl_url and ("general" in title_d or "csv" in media or "zip" in media):
                    return dl_url, str(year)
        except Exception:
            continue

    return None


class CmsOpenPaymentsScraper(BaseScraper):
    source_id = "cms_open_payments"
    schedule = "0 5 15 * *"   # monthly
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        last_period = state.get("cursor")

        with make_client(timeout=60, headers=_HEADERS) as client:
            try:
                resp = client.get(_METADATA_URL)
                resp.raise_for_status()
                datasets = resp.json()
            except Exception:
                datasets = []

        result = _find_general_payments_url(datasets)
        if not result:
            return []  # No dataset found — skip until next scheduled run
        url, period = result

        if last_period == period:
            return []

        return [
            DownloadTarget(
                url=url,
                source_id=self.source_id,
                period=period,
                doc_type="zip" if url.lower().endswith(".zip") else "csv",
                filename=f"cms_open_payments_{period}.zip",
                metadata={"cursor_after": period, "program_year": period},
            )
        ]

    def fetch(self, target: DownloadTarget) -> bytes:
        """Not used — fetch_to_file() handles the streaming download."""
        raise NotImplementedError("CMS uses fetch_to_file() streaming path")

    @retry(stop=stop_after_attempt(2), wait=wait_exponential(min=10, max=120), reraise=True)
    def fetch_to_file(self, target: DownloadTarget) -> Path:
        """Stream the CMS CSV directly to a temp file (avoids loading 9 GB into RAM)."""
        tmp_dir = Path(tempfile.mkdtemp())
        dest = tmp_dir / target.filename
        with make_client(timeout=7200, headers=_HEADERS) as client:
            with client.stream("GET", target.url) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(chunk_size=8 * 1024 * 1024):
                        f.write(chunk)
        return dest

    def parse_to_db(self, raw: bytes | None, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert CMS physician payments using PostgreSQL COPY.

        raw is None when the streaming fetch_to_file() path was used;
        the file path is in target.metadata["_local_path"].
        """
        local_path = target.metadata.get("_local_path")
        if local_path:
            csv_path = _prepare_csv_from_path(Path(local_path))
            _tmp_dir = None
        else:
            assert raw is not None
            _tmp_dir = tempfile.mkdtemp()
            csv_path = _extract_csv(raw, _tmp_dir)

        try:
            rows = _build_cms_rows(csv_path, target)
            if not rows:
                return 0, 0
            with get_conn() as conn:
                inserted, total = bulk_insert_individuals(conn, doc_id, "cms_payment_recipient", rows)
            return total, inserted
        finally:
            if _tmp_dir:
                shutil.rmtree(_tmp_dir, ignore_errors=True)

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Fallback parse — not used (parse_to_db takes priority)."""
        return []


def _prepare_csv_from_path(path: Path) -> str:
    """Return the CSV path for a file already on disk (no extraction needed for plain CSV)."""
    # If it's a ZIP, extract into the same directory
    if path.suffix.lower() == ".zip":
        tmp_dir = str(path.parent)
        with open(path, "rb") as f:
            return _extract_csv(f.read(), tmp_dir)
    # Already a plain CSV
    return str(path)


def _extract_csv(raw: bytes, tmp_dir: str) -> str:
    """Extract CSV from ZIP (or return raw CSV) into tmp_dir."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            csv_names = [n for n in zf.namelist()
                         if n.lower().endswith(".csv")
                         and "general" in n.lower()
                         and "detail" in n.lower()]
            if not csv_names:
                csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not csv_names:
                raise ValueError(f"No CSV found in ZIP: {zf.namelist()}")

            out_path = os.path.join(tmp_dir, csv_names[0].split("/")[-1])
            with zf.open(csv_names[0]) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return out_path
    except zipfile.BadZipFile:
        # Already a raw CSV
        out_path = os.path.join(tmp_dir, "cms_data.csv")
        with open(out_path, "wb") as f:
            f.write(raw)
        return out_path


def _build_cms_rows(path: str, target: DownloadTarget) -> list[dict]:
    """Stream CMS CSV → unique physicians → row dicts for bulk_insert_individuals."""
    records = _parse_cms_csv(path, target)
    rows = []
    for rec in records:
        p = rec.individuals[0]
        rows.append({
            "name": p.name,
            "name_norm": normalize_name(p.name),
            "excerpt": p.excerpt or "",
            "identifiers": p.identifiers or {},
        })
    return rows


def _parse_cms_csv(path: str, target: DownloadTarget) -> list[ParsedRecord]:
    """Stream the CMS CSV with Polars lazy scanning, extract physician recipients."""

    # Detect available columns
    header_df = pl.read_csv(path, n_rows=0, infer_schema_length=0)
    available = set(header_df.columns)

    first_col = next((c for c in _PHYSICIAN_COLS if c in available), None)
    if not first_col:
        # Try case-insensitive match
        col_map = {c.lower(): c for c in available}
        physician_cols_lower = {c.lower(): c for c in _PHYSICIAN_COLS}
        col_map.update(physician_cols_lower)

    select_cols = [c for c in _PHYSICIAN_COLS if c in available]
    if not select_cols:
        return []

    # Must have at least last name to identify individuals
    last_name_col = next(
        (c for c in available if "last_name" in c.lower() and "recipient" in c.lower()), None
    )
    if not last_name_col:
        return []

    first_name_col = next(
        (c for c in available if "first_name" in c.lower() and "recipient" in c.lower()), None
    )
    mid_name_col = next(
        (c for c in available if "middle_name" in c.lower() and "recipient" in c.lower()), None
    )
    profile_id_col = next(
        (c for c in available if "profile_id" in c.lower() and "recipient" in c.lower()), None
    )
    npi_col = next((c for c in available if "npi" in c.lower() and "recipient" in c.lower()), None)
    specialty_col = next((c for c in available if "specialty" in c.lower()), None)
    state_col = next((c for c in available if "recipient_state" in c.lower()), None)
    amount_col = next((c for c in available if "amount_of_payment" in c.lower()), None)
    mfr_col = next((c for c in available if "making_payment_name" in c.lower()), None)

    build_cols = [c for c in [last_name_col, first_name_col, mid_name_col,
                               profile_id_col, npi_col, specialty_col,
                               state_col, amount_col, mfr_col] if c]

    df = (
        pl.scan_csv(
            path,
            has_header=True,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
            encoding="utf8-lossy",
        )
        .filter(pl.col(last_name_col).is_not_null() & (pl.col(last_name_col) != ""))
        .select(build_cols)
        .collect(engine="streaming")
    )

    records: list[ParsedRecord] = []
    for row in df.iter_rows(named=True):
        last = (row.get(last_name_col) or "").strip()
        if not last:
            continue
        first = (row.get(first_name_col) or "").strip() if first_name_col else ""
        mid = (row.get(mid_name_col) or "").strip() if mid_name_col else ""
        name = " ".join(p for p in [first, mid, last] if p)

        # Skip placeholder names that normalize to empty (e.g. ". .", ".", " ")
        if not normalize_name(name):
            continue

        profile_id = (row.get(profile_id_col) or "").strip() if profile_id_col else ""
        npi = (row.get(npi_col) or "").strip() if npi_col else ""
        specialty = (row.get(specialty_col) or "").strip() if specialty_col else ""
        state = (row.get(state_col) or "").strip() if state_col else ""
        amount = (row.get(amount_col) or "").strip() if amount_col else ""
        mfr = (row.get(mfr_col) or "").strip() if mfr_col else ""

        identifiers: dict = {}
        if profile_id:
            identifiers["cms_profile_id"] = profile_id
        if npi:
            identifiers["npi"] = npi
        if specialty:
            identifiers["specialty"] = specialty
        if state:
            identifiers["state"] = state

        excerpt_parts = [f"CMS Open Payments {target.period}"]
        if specialty:
            excerpt_parts.append(specialty)
        if amount:
            try:
                excerpt_parts.append(f"${float(amount):,.2f}")
            except (ValueError, TypeError):
                pass
        if mfr:
            excerpt_parts.append(f"from {mfr[:60]}")

        records.append(
            ParsedRecord(
                individuals=[
                    ParsedIndividual(
                        name=name,
                        relationship="cms_payment_recipient",
                        excerpt=" | ".join(excerpt_parts),
                        identifiers=identifiers,
                    )
                ],
                source_identifier=profile_id or npi or name,
            )
        )

    return records
