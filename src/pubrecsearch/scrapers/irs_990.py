"""IRS Form 990 scraper — officer/director extraction.

Source: https://apps.irs.gov/pub/epostcard/990/xml/
Format: Annual CSV index + monthly batch ZIP bundles (TEOS EFILE system)
Update frequency: Monthly new batch ZIPs per year
Schedule: Monthly (0 6 10 * *)
Relationship type: irs990_officer

IRS migrated from the old S3 bucket (s3://irs-form-990/) to TEOS in 2025.
New URL structure:
  Index CSV:  https://apps.irs.gov/pub/epostcard/990/xml/{year}/index_{year}.csv
  Batch ZIPs: https://apps.irs.gov/pub/epostcard/990/xml/{year}/{batch_id}.zip
              where batch_id = e.g. "2026_TEOS_XML_01A"

Each batch ZIP contains ~5,000–15,000 individual XML files named
{OBJECT_ID}_public.xml.  The index CSV maps each filing's OBJECT_ID to its
XML_BATCH_ID so we know which ZIP to fetch.

discover() fetches the current year's index CSV, finds batch IDs not yet
processed (tracked in state metadata), and returns one DownloadTarget per
new batch ZIP.  parse() opens the ZIP and extracts officers from every XML.

Historical backfill: scripts/bootstrap/irs990_bootstrap.py
"""

import csv
import io
import zipfile
from datetime import date

from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..db import bulk_insert_individuals, get_conn
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

# New TEOS base URL (replaced s3://irs-form-990/)
_BASE_URL = "https://apps.irs.gov/pub/epostcard/990/xml"
_INDEX_URL = _BASE_URL + "/{year}/index_{year}.csv"
_BATCH_URL = _BASE_URL + "/{year}/{batch_id}.zip"

_HEADERS = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}

# Maximum new batch ZIPs to queue per monthly run (each ZIP ~70MB, ~12K XMLs)
_MAX_BATCHES_PER_RUN = 5


# ---------------------------------------------------------------------------
# XML parsing helpers (unchanged from old scraper)
# ---------------------------------------------------------------------------

import re
from xml.etree import ElementTree as ET

_NS_RE = re.compile(r"\{[^}]+\}")


def _strip_ns(tag: str) -> str:
    return _NS_RE.sub("", tag)


def _find_text(elem: ET.Element, *tags: str) -> str:
    for child in elem.iter():
        if _strip_ns(child.tag) in tags:
            return (child.text or "").strip()
    return ""


def _extract_officers(root: ET.Element) -> list[tuple[str, str, str]]:
    """Return list of (name, title, compensation) for officers in a 990 XML."""
    results = []
    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        if tag in ("OfficerDirectorTrusteeKeyEmplGrp", "KeyEmployeeGrp",
                   "HighestCompensatedEmployeeGrp"):
            name = _find_text(elem, "PersonNm")
            title = _find_text(elem, "TitleTxt")
            comp = _find_text(elem, "ReportableCompFromOrgAmt", "TotalCompensationAmt")
            if name:
                results.append((name, title, comp))
        elif tag == "Form990PartVIISectionA":
            name = _find_text(elem, "NameOfOfficer", "PersonNm")
            title = _find_text(elem, "TitleOfOfficer", "TitleTxt")
            comp = _find_text(elem, "CompensationAmount", "ReportableCompFromOrgAmt")
            if name:
                results.append((name, title, comp))
        elif tag == "OfficerDirectorTrusteeEmplGrp":
            name = _find_text(elem, "PersonNm")
            title = _find_text(elem, "TitleTxt")
            comp = _find_text(elem, "CompensationAmt")
            if name:
                results.append((name, title, comp))
    return results


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class Irs990Scraper(BaseScraper):
    source_id = "irs_990"
    schedule = "0 6 10 * *"   # 10th of each month at 6 AM
    doc_type = "zip"

    def discover(self, state: dict) -> list[DownloadTarget]:
        year = date.today().year
        # cursor = last processed batch_id (e.g. "2026_TEOS_XML_03A")
        # Batch IDs are lexicographically ordered (YYYY_TEOS_XML_NNL), so
        # a simple string comparison identifies new batches.
        last_cursor = state.get("cursor") or ""

        index_url = _INDEX_URL.format(year=year)
        with make_client(timeout=60, headers=_HEADERS) as client:
            resp = client.get(index_url)
            if resp.status_code == 404:
                # Current year index not published yet — try prior year
                year -= 1
                index_url = _INDEX_URL.format(year=year)
                resp = client.get(index_url)
            resp.raise_for_status()

        # Parse CSV index: collect unique batch IDs newer than cursor
        reader = csv.DictReader(io.StringIO(resp.text))
        batch_ids: dict[str, dict] = {}
        for row in reader:
            batch_id = row.get("XML_BATCH_ID", "").strip()
            if not batch_id or batch_id <= last_cursor:
                continue
            if batch_id not in batch_ids:
                batch_ids[batch_id] = {
                    "year": str(year),
                    "tax_period": row.get("TAX_PERIOD", "").strip(),
                }

        # Process in chronological order, cap at _MAX_BATCHES_PER_RUN per job
        new_batches = sorted(batch_ids.keys())[:_MAX_BATCHES_PER_RUN]

        targets = []
        for batch_id in new_batches:
            meta = batch_ids[batch_id]
            tax_period = meta["tax_period"]
            period = f"{tax_period[:4]}-{tax_period[4:6]}" if len(tax_period) >= 6 else str(year)
            url = _BATCH_URL.format(year=meta["year"], batch_id=batch_id)
            targets.append(
                DownloadTarget(
                    url=url,
                    source_id=self.source_id,
                    period=period,
                    doc_type=self.doc_type,
                    filename=f"{batch_id}.zip",
                    metadata={
                        "batch_id": batch_id,
                        "year": meta["year"],
                        "cursor_after": batch_id,
                    },
                )
            )

        return targets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=300, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse_to_db(self, raw: bytes, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert officers from all XMLs in the batch ZIP."""
        batch_id = target.metadata.get("batch_id", target.filename)

        rows = []
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                for name in zf.namelist():
                    if not name.lower().endswith(".xml"):
                        continue
                    try:
                        xml_bytes = zf.read(name)
                        root = ET.fromstring(xml_bytes)
                    except Exception:
                        continue

                    # Extract org name and EIN from return header for context
                    org_name = _find_text(root, "BusinessName", "BusinessNameLine1Txt",
                                          "BusinessNameLine1")
                    ein = _find_text(root, "EIN", "Filer")
                    form_type = _find_text(root, "ReturnTypeCd")
                    tax_period = _find_text(root, "TaxPeriodEndDt")[:7].replace("-", "")  # YYYYMM

                    for officer_name, title, compensation in _extract_officers(root):
                        if not officer_name:
                            continue
                        identifiers: dict = {}
                        if ein:
                            identifiers["ein"] = ein
                        if title:
                            identifiers["title"] = title
                        if org_name:
                            identifiers["org"] = org_name[:100]

                        excerpt_parts = [f"IRS 990 {form_type or 'filing'}"]
                        if title:
                            excerpt_parts.append(title)
                        if org_name:
                            excerpt_parts.append(f"at {org_name[:60]}")
                        if compensation:
                            try:
                                excerpt_parts.append(f"comp: ${int(float(compensation)):,}")
                            except (ValueError, TypeError):
                                pass

                        rows.append({
                            "name": officer_name,
                            "name_norm": normalize_name(officer_name),
                            "excerpt": "; ".join(excerpt_parts),
                            "identifiers": identifiers,
                        })
        except zipfile.BadZipFile as exc:
            raise RuntimeError(f"Bad ZIP for batch {batch_id}: {exc}") from exc

        if not rows:
            return 0, 0

        with get_conn() as conn:
            inserted, total = bulk_insert_individuals(conn, doc_id, "irs990_officer", rows)
        return total, inserted

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Fallback parse — not used (parse_to_db takes priority)."""
        return []


# ---------------------------------------------------------------------------
# Helpers for bootstrap script
# ---------------------------------------------------------------------------

def fetch_year_index(year: int) -> list[dict]:
    """Download and return the IRS 990 index CSV rows for a given year."""
    url = _INDEX_URL.format(year=year)
    with make_client(timeout=120, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)


def get_batch_ids_for_year(year: int) -> list[str]:
    """Return sorted list of unique batch IDs in the year's index."""
    rows = fetch_year_index(year)
    seen: set[str] = set()
    result = []
    for row in rows:
        bid = row.get("XML_BATCH_ID", "").strip()
        if bid and bid not in seen:
            seen.add(bid)
            result.append(bid)
    return sorted(result)


def fetch_batch_zip(year: int, batch_id: str) -> bytes:
    """Download a batch ZIP — used by irs990_bootstrap.py."""
    url = _BATCH_URL.format(year=year, batch_id=batch_id)
    with make_client(timeout=300, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content
