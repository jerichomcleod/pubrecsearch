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
    """Scan dataset metadata to find the latest General Payments bulk download."""
    candidates = []
    for ds in datasets:
        title = (ds.get("title") or "").lower()
        if "general payment" in title and "detail" in title:
            # Extract program year from title (e.g. "2023")
            for word in (ds.get("title") or "").split():
                if word.isdigit() and 2013 <= int(word) <= date.today().year:
                    year = int(word)
                    # Find download URL
                    for dist in (ds.get("distribution") or []):
                        if dist.get("mediaType") in ("text/csv", "application/zip"):
                            dl_url = dist.get("downloadURL") or dist.get("accessURL")
                            if dl_url:
                                candidates.append((year, dl_url))
                    break

    if not candidates:
        return None
    # Return the most recent year
    candidates.sort(key=lambda x: x[0], reverse=True)
    year, url = candidates[0]
    return url, str(year)


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
            # Fallback: use a known stable URL pattern for the most recent year
            year = date.today().year - 1  # CMS publishes prior year ~June
            period = str(year)
            # CMS stable URL pattern (may need updating annually)
            url = (
                f"https://download.cms.gov/openpayments/PGYR{str(year)[2:]}_P06282024.ZIP"
            )
        else:
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

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=120), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=600, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse_to_db(self, raw: bytes, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert CMS physician payments using PostgreSQL COPY."""
        tmp_dir = tempfile.mkdtemp()
        try:
            csv_path = _extract_csv(raw, tmp_dir)
            rows = _build_cms_rows(csv_path, target)
            if not rows:
                return 0, 0
            with get_conn() as conn:
                inserted, total = bulk_insert_individuals(conn, doc_id, "cms_payment_recipient", rows)
            return total, inserted
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Fallback parse — not used (parse_to_db takes priority)."""
        return []


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
