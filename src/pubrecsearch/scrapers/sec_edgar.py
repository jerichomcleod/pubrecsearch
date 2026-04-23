"""SEC EDGAR Form 4 / Form 3 / SC 13G scraper.

Source: https://www.sec.gov/Archives/edgar/full-index/
Format: Quarterly fixed-width index files (form.gz)
Update frequency: Quarterly index; daily new filings
Schedule: Weekly (0 3 * * 1) — downloads the latest daily index entries
Relationship type: sec_insider (Form 4/3), sec_beneficial_owner (SC 13G/A)

For Form 4 and 3, the "company name" in the EDGAR index IS the reporting person
(the individual insider), not the issuer. For SC 13G, it may be an institution —
entity-pattern filtering is applied to skip non-individuals.

Ongoing scraper: processes daily index files for the current week.
Bootstrap: edgar_bootstrap.py processes quarterly archives from 2015 onward.
"""

import gzip
import io
import re
from datetime import date, timedelta

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

_BASE_URL = "https://www.sec.gov/Archives/edgar/full-index"
_DAILY_BASE = "https://www.sec.gov/Archives/edgar/daily-index"

_TARGET_FORMS = {"4", "4/A", "3", "3/A", "SC 13G", "SC 13G/A"}

_ENTITY_PATTERNS = re.compile(
    r"\b(llc|l\.l\.c|inc|corp|ltd|lp|llp|pllc|trust|fund|partners|"
    r"management|capital|advisors|associates|holdings|group|company|"
    r"international|bank|financial|securities|investments?)\b",
    re.IGNORECASE,
)

# SEC requires a descriptive User-Agent per their robots policy
_HEADERS = {"User-Agent": "PubRecSearch research@example.com"}

# Relationship type by form
_FORM_RELATIONSHIPS = {
    "4": "sec_insider",
    "4/A": "sec_insider",
    "3": "sec_insider",
    "3/A": "sec_insider",
    "SC 13G": "sec_beneficial_owner",
    "SC 13G/A": "sec_beneficial_owner",
}


def _quarter(d: date) -> tuple[int, int]:
    return d.year, (d.month - 1) // 3 + 1


def _daily_url(d: date) -> str:
    y, q = _quarter(d)
    return f"{_DAILY_BASE}/{y}/QTR{q}/form{d.strftime('%Y%m%d')}.gz"


def _quarterly_url(year: int, qtr: int) -> str:
    return f"{_BASE_URL}/{year}/QTR{qtr}/form.gz"


class SecEdgarScraper(BaseScraper):
    source_id = "sec_edgar"
    schedule = "0 3 * * 1"   # weekly on Monday
    doc_type = "gz"

    def discover(self, state: dict) -> list[DownloadTarget]:
        """Return daily index files for the past 7 days not yet processed."""
        last_date_str = state.get("cursor")
        last_date = (
            date.fromisoformat(last_date_str) if last_date_str else date.today() - timedelta(days=7)
        )

        today = date.today()
        targets = []

        with make_client(timeout=30, headers=_HEADERS) as client:
            d = last_date + timedelta(days=1)
            while d <= today:
                if d.weekday() < 5:  # weekdays only
                    url = _daily_url(d)
                    try:
                        resp = client.head(url)
                        if resp.status_code == 200:
                            targets.append(
                                DownloadTarget(
                                    url=url,
                                    source_id=self.source_id,
                                    period=d.isoformat(),
                                    doc_type=self.doc_type,
                                    filename=f"edgar_daily_{d.isoformat()}.gz",
                                    metadata={"cursor_after": d.isoformat(), "index_date": d.isoformat()},
                                )
                            )
                    except Exception:
                        pass
                d += timedelta(days=1)

        return targets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=120, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        try:
            text = gzip.decompress(raw).decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")

        return _parse_index_text(text, target)


def _parse_index_text(text: str, target: DownloadTarget) -> list[ParsedRecord]:
    """Parse EDGAR fixed-width index text, return records for target form types."""
    lines = text.splitlines()

    # Skip header lines (before the dashes separator)
    data_start = 0
    for i, line in enumerate(lines):
        if line.startswith("---"):
            data_start = i + 1
            break

    records: list[ParsedRecord] = []

    for line in lines[data_start:]:
        if not line.strip():
            continue

        # Fixed-width: Form Type (0-20), Company Name (20-82), CIK (82-92),
        # Date Filed (92-102), Filename (102+)
        if len(line) < 102:
            continue

        form_type = line[0:20].strip()
        company_name = line[20:82].strip()
        cik = line[82:92].strip()
        date_filed = line[92:102].strip()
        filename = line[102:].strip()

        if form_type not in _TARGET_FORMS:
            continue

        if not company_name:
            continue

        # Skip institutional filers for SC 13G (keep individuals)
        if form_type in ("SC 13G", "SC 13G/A") and _ENTITY_PATTERNS.search(company_name):
            continue

        relationship = _FORM_RELATIONSHIPS.get(form_type, "sec_insider")

        identifiers: dict = {}
        if cik:
            identifiers["cik"] = cik
        if date_filed:
            identifiers["date_filed"] = date_filed
        if form_type:
            identifiers["form_type"] = form_type

        accession = filename.replace("/", "-").replace(".txt", "").replace("-index.htm", "")

        records.append(
            ParsedRecord(
                individuals=[
                    ParsedIndividual(
                        name=company_name,
                        relationship=relationship,
                        excerpt=f"SEC {form_type} filed {date_filed}",
                        identifiers=identifiers,
                    )
                ],
                source_identifier=accession or f"{cik}_{date_filed}",
            )
        )

    return records


# ---------------------------------------------------------------------------
# Helpers used by bootstrap script
# ---------------------------------------------------------------------------


def fetch_quarterly_index(year: int, qtr: int) -> bytes:
    """Download a quarterly form.gz index — used by edgar_bootstrap.py."""
    url = _quarterly_url(year, qtr)
    with make_client(timeout=120, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def parse_quarterly_index(raw: bytes, year: int, qtr: int) -> list[ParsedRecord]:
    """Parse a quarterly form.gz index — used by edgar_bootstrap.py."""
    try:
        text = gzip.decompress(raw).decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    dummy_target = DownloadTarget(
        url="",
        source_id="sec_edgar",
        period=f"{year}-Q{qtr}",
        doc_type="gz",
        filename=f"edgar_{year}_Q{qtr}.gz",
    )
    return _parse_index_text(text, dummy_target)
