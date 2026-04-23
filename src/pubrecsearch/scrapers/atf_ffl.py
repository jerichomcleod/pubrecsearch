"""ATF Federal Firearms Licensee (FFL) List scraper.

Source: https://www.atf.gov/firearms/listing-federal-firearms-licensees
Format: Monthly text files, comma-delimited
Update frequency: Monthly
Relationship type: ffl_licensee

Sole proprietors are identified by license type (01, 02, 07, 08, 09, 10, 11)
combined with the business name matching an individual name pattern (no legal
entity indicators like LLC/Inc/Corp). ATF publishes ~128K active licensees.
"""

import io
import re
from calendar import monthrange
from datetime import date, timedelta

import httpx
import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

# ATF publishes monthly CSVs at a predictable URL:
# https://www.atf.gov/sites/default/files2/ffl/{MMYY}-ffl-list.csv
# where MMYY = zero-padded month + 2-digit year, e.g. "0126" = January 2026.
# ATF often publishes 1-2 months behind, so we walk back up to 3 months.
_BASE_URL = "https://www.atf.gov/sites/default/files2/ffl"
_MAX_MONTHS_BACK = 3

# Akamai CDN requires a real User-Agent on direct file requests
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/147.0.0.0 Safari/537.36"
    ),
}

# License types that can be held by individuals (sole proprietors)
_INDIVIDUAL_LICENSE_TYPES = {"01", "02", "03", "06", "07", "08", "09", "10", "11"}

# Patterns indicating a business entity (not an individual)
_ENTITY_PATTERNS = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co\.|ltd|limited|"
    r"lp|llp|pllc|trust|foundation|church|association|assoc|group|holdings|"
    r"enterprises|services|solutions|industries|international|company)\b",
    re.IGNORECASE,
)

# Columns in ATF FFL text file (space-padded fixed-width or comma-delimited)
_COLUMNS = [
    "lic_regn", "lic_dist", "lic_cnty", "lic_type", "lic_xprdte",
    "lic_seqn", "licensee_name", "business_name", "premise_street",
    "premise_city", "premise_state", "premise_zip_code", "mail_street",
    "mail_city", "mail_state", "mail_zip_code", "voice_phone",
]


class AtfFflScraper(BaseScraper):
    source_id = "atf_ffl"
    schedule = "0 4 1 * *"   # 1st of each month at 4 AM
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        last_period = state.get("cursor")

        # Walk back up to _MAX_MONTHS_BACK months to find the latest published file.
        # ATF typically publishes 1-2 months behind the current date.
        today = date.today()
        with make_client(timeout=30, headers=_HEADERS) as client:
            for months_back in range(0, _MAX_MONTHS_BACK + 1):
                candidate = _month_offset(today, -months_back)
                period = candidate.strftime("%Y-%m")

                if period == last_period:
                    return []  # already have the most recent available file

                mmyy = f"{candidate.month:02d}{str(candidate.year)[2:]}"
                url = f"{_BASE_URL}/{mmyy}-ffl-list.csv"

                resp = client.head(url)
                if resp.status_code == 200:
                    return [
                        DownloadTarget(
                            url=url,
                            source_id=self.source_id,
                            period=period,
                            doc_type=self.doc_type,
                            filename=f"atf_ffl_{period}.csv",
                            metadata={"cursor_after": period},
                        )
                    ]

        return []  # nothing found within lookback window

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=120, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        # ATF files are comma-delimited text with a header row
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        df = pl.read_csv(
            io.StringIO(text),
            has_header=True,
            infer_schema_length=0,  # all strings
            ignore_errors=True,
            truncate_ragged_lines=True,
        )

        # Normalize column names — ATF sometimes ships varying headers
        df = df.rename({c: c.strip().lower().replace(" ", "_") for c in df.columns})

        # Map to our expected column names by position if header is missing/malformed
        if "lic_type" not in df.columns and len(df.columns) >= 4:
            rename_map = {df.columns[i]: _COLUMNS[i] for i in range(min(len(_COLUMNS), len(df.columns)))}
            df = df.rename(rename_map)

        records: list[ParsedRecord] = []

        for row in df.iter_rows(named=True):
            lic_type = (row.get("lic_type") or "").strip()
            business_name = (row.get("business_name") or row.get("licensee_name") or "").strip()
            licensee_name = (row.get("licensee_name") or "").strip()

            if not business_name and not licensee_name:
                continue

            # Use business_name as primary display name; fall back to licensee_name
            name = business_name or licensee_name

            # Filter to individual license types only
            if lic_type not in _INDIVIDUAL_LICENSE_TYPES:
                continue

            # Skip obvious business entities
            if _ENTITY_PATTERNS.search(name):
                continue

            seqn = (row.get("lic_seqn") or "").strip()
            regn = (row.get("lic_regn") or "").strip()
            dist = (row.get("lic_dist") or "").strip()
            state = (row.get("premise_state") or "").strip()
            city = (row.get("premise_city") or "").strip()

            lic_number = f"{regn}-{dist}-{lic_type}-{seqn}" if regn else seqn

            records.append(
                ParsedRecord(
                    individuals=[
                        ParsedIndividual(
                            name=name,
                            relationship="ffl_licensee",
                            excerpt=f"ATF FFL {lic_number} in {city}, {state}",
                            identifiers={
                                "lic_number": lic_number,
                                "lic_type": lic_type,
                                "state": state,
                                "city": city,
                            },
                        )
                    ],
                    source_identifier=lic_number,
                )
            )

        return records


def _month_offset(d: date, months: int) -> date:
    """Return a date shifted by `months` calendar months (negative = back)."""
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)
