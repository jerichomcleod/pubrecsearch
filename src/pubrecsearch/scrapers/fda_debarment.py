"""FDA Debarment List scraper.

Source: https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/debarment-list
Format: HTML page with a structured table (rarely changes)
Update frequency: Ad-hoc (handful of additions per year)
Relationship type: fda_debarred_individual

Re-parses only when page content hash changes.
"""

import hashlib
import re
from datetime import date

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

_URL = (
    "https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations"
    "/compliance-actions-and-activities/fda-debarment-list-drug-product-applications"
)


class FdaDebarmentScraper(BaseScraper):
    source_id = "fda_debarment"
    schedule = "0 7 * * 1"   # weekly on Monday at 7 AM
    doc_type = "html"

    def discover(self, state: dict) -> list[DownloadTarget]:
        today = date.today().isoformat()
        return [
            DownloadTarget(
                url=_URL,
                source_id=self.source_id,
                period=today,
                doc_type=self.doc_type,
                filename=f"fda_debarment_{today}.html",
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=30) as client:
            resp = client.get(target.url, headers=target.headers)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            html = raw.decode("latin-1")

        soup = BeautifulSoup(html, "lxml")
        records: list[ParsedRecord] = []

        # The FDA debarment page contains tables with columns like:
        # Name | Effective Date | Permanent/Duration | Basis
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]

            # Identify name column
            name_col = _find_col(headers, ("name", "individual", "person"))
            if name_col is None:
                continue

            date_col = _find_col(headers, ("effective", "date", "debarment date"))
            basis_col = _find_col(headers, ("basis", "section", "reason", "statutory"))
            duration_col = _find_col(headers, ("permanent", "duration", "period", "term"))

            for tr in table.find_all("tr"):
                cells = tr.find_all(["td", "th"])
                if not cells or len(cells) <= name_col:
                    continue
                # Skip header rows
                if cells[name_col].name == "th":
                    continue

                name = cells[name_col].get_text(strip=True)
                if not name or name.lower() in ("name", "individual"):
                    continue

                # Skip obvious non-person entries (company names)
                if re.search(
                    r"\b(inc|llc|corp|ltd|co\.|company|corporation|group)\b",
                    name,
                    re.IGNORECASE,
                ):
                    continue

                eff_date = _cell_text(cells, date_col)
                basis = _cell_text(cells, basis_col)
                duration = _cell_text(cells, duration_col)

                identifiers: dict = {}
                if eff_date:
                    identifiers["effective_date"] = eff_date
                if basis:
                    identifiers["basis"] = basis

                excerpt_parts = ["FDA debarred"]
                if eff_date:
                    excerpt_parts.append(f"effective {eff_date}")
                if duration:
                    excerpt_parts.append(f"({duration})")
                if basis:
                    excerpt_parts.append(f"basis: {basis}")

                records.append(
                    ParsedRecord(
                        individuals=[
                            ParsedIndividual(
                                name=name,
                                relationship="fda_debarred_individual",
                                excerpt="; ".join(excerpt_parts),
                                identifiers=identifiers,
                            )
                        ],
                        source_identifier=f"{name}_{eff_date or 'unknown'}",
                    )
                )

        return records


def _find_col(headers: list[str], keywords: tuple[str, ...]) -> int | None:
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def _cell_text(cells: list, idx: int | None) -> str | None:
    if idx is None or idx >= len(cells):
        return None
    text = cells[idx].get_text(strip=True)
    return text if text else None
