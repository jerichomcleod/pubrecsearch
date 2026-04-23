"""IRS Form 990 scraper — officer/director extraction.

Source: IRS public S3 bucket (s3://irs-form-990/)
Index: https://s3.amazonaws.com/irs-form-990/index_{YEAR}.json
Format: Individual XML files per filing; index JSON lists all filings for a year
Update frequency: Continuous; indexed monthly
Schedule: Monthly (0 6 10 * *)
Relationship type: irs990_officer

discover() fetches the current year's index JSON, filters to filings with
LastUpdated > cursor, and returns one DownloadTarget per XML filing.
parse() extracts officer/director/trustee names from each XML.

For historical backfill (2015–present), use scripts/bootstrap/irs990_bootstrap.py.
"""

import json
import re
from datetime import date, datetime, timezone
from xml.etree import ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

_INDEX_URL = "https://s3.amazonaws.com/irs-form-990/index_{year}.json"
_HEADERS = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}

# Max filings to queue per run (prevents 50K+ target batches on first run)
_MAX_TARGETS_PER_RUN = 5000

# XML namespace prefix used in 990 schema (varies; strip dynamically)
_NS_RE = re.compile(r"\{[^}]+\}")


def _strip_ns(tag: str) -> str:
    return _NS_RE.sub("", tag)


def _find_text(elem: ET.Element, *tags: str) -> str:
    """Find first matching child element by tag name (namespace-agnostic)."""
    for child in elem.iter():
        if _strip_ns(child.tag) in tags:
            return (child.text or "").strip()
    return ""


class Irs990Scraper(BaseScraper):
    source_id = "irs_990"
    schedule = "0 6 10 * *"   # 10th of each month at 6 AM
    doc_type = "xml"

    def discover(self, state: dict) -> list[DownloadTarget]:
        year = date.today().year
        last_updated = state.get("cursor")  # ISO datetime string

        index_url = _INDEX_URL.format(year=year)
        with make_client(timeout=120, headers=_HEADERS) as client:
            resp = client.get(index_url)
            if resp.status_code == 404:
                # IRS may not have current year index yet; try prior year
                year -= 1
                index_url = _INDEX_URL.format(year=year)
                resp = client.get(index_url)
            resp.raise_for_status()
            entries = resp.json().get("Filings990", [])

        targets = []
        for entry in entries:
            updated = entry.get("LastUpdated", "")
            if last_updated and updated <= last_updated:
                continue

            url = entry.get("URL", "")
            if not url:
                continue

            ein = entry.get("EIN", "").strip()
            form_type = entry.get("FormType", "").strip()
            company = entry.get("CompanyName", "").strip()
            tax_period = entry.get("TaxPeriod", "").strip()  # e.g. "202312"
            period = f"{tax_period[:4]}-{tax_period[4:6]}" if len(tax_period) >= 6 else str(year)

            targets.append(
                DownloadTarget(
                    url=url,
                    source_id=self.source_id,
                    period=period,
                    doc_type=self.doc_type,
                    filename=url.split("/")[-1],
                    metadata={
                        "cursor_after": updated,
                        "ein": ein,
                        "company": company,
                        "form_type": form_type,
                    },
                )
            )

            if len(targets) >= _MAX_TARGETS_PER_RUN:
                break

        return targets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=60, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        ein = target.metadata.get("ein", "")
        company = target.metadata.get("company", "")
        form_type = target.metadata.get("form_type", "990")

        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []

        officers = _extract_officers(root)
        if not officers:
            return []

        records = []
        for officer_name, title, compensation in officers:
            if not officer_name:
                continue

            identifiers: dict = {}
            if ein:
                identifiers["ein"] = ein
            if title:
                identifiers["title"] = title
            if company:
                identifiers["org"] = company

            excerpt_parts = [f"IRS 990 {form_type}"]
            if title:
                excerpt_parts.append(title)
            if company:
                excerpt_parts.append(f"at {company}")
            if compensation:
                try:
                    excerpt_parts.append(f"comp: ${int(float(compensation)):,}")
                except (ValueError, TypeError):
                    pass

            records.append(
                ParsedRecord(
                    individuals=[
                        ParsedIndividual(
                            name=officer_name,
                            relationship="irs990_officer",
                            excerpt="; ".join(excerpt_parts),
                            identifiers=identifiers,
                        )
                    ],
                    source_identifier=f"{ein}_{officer_name}",
                )
            )

        return records


def _extract_officers(root: ET.Element) -> list[tuple[str, str, str]]:
    """Return list of (name, title, compensation) for officers in a 990 XML."""
    results = []

    for elem in root.iter():
        tag = _strip_ns(elem.tag)

        # Modern schema (2013+): OfficerDirectorTrusteeKeyEmplGrp
        if tag in ("OfficerDirectorTrusteeKeyEmplGrp", "KeyEmployeeGrp",
                   "HighestCompensatedEmployeeGrp"):
            name = _find_text(elem, "PersonNm")
            title = _find_text(elem, "TitleTxt")
            comp = _find_text(elem, "ReportableCompFromOrgAmt", "TotalCompensationAmt")
            if name:
                results.append((name, title, comp))

        # Legacy schema: Form990PartVIISectionA
        elif tag == "Form990PartVIISectionA":
            name = _find_text(elem, "NameOfOfficer", "PersonNm")
            title = _find_text(elem, "TitleOfOfficer", "TitleTxt")
            comp = _find_text(elem, "CompensationAmount", "ReportableCompFromOrgAmt")
            if name:
                results.append((name, title, comp))

        # 990-EZ officers
        elif tag == "OfficerDirectorTrusteeEmplGrp":
            name = _find_text(elem, "PersonNm")
            title = _find_text(elem, "TitleTxt")
            comp = _find_text(elem, "CompensationAmt")
            if name:
                results.append((name, title, comp))

    return results


# ---------------------------------------------------------------------------
# Helper for bootstrap script
# ---------------------------------------------------------------------------


def fetch_year_index(year: int) -> list[dict]:
    """Download and return the IRS 990 index for a given year."""
    url = _INDEX_URL.format(year=year)
    with make_client(timeout=120, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json().get("Filings990", [])
