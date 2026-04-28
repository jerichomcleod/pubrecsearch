"""ATF Federal Firearms Licensee (FFL) List scraper.

Source: https://www.atf.gov/firearms/tools-and-services-firearms-industry/federal-firearms-listings
Format: Monthly CSV (complete all-states listing)
Update frequency: Monthly (available ~1 month behind current date)
Schedule: Monthly (0 4 1 * *)
Relationship type: ffl_licensee

ATF's FFL listing page is behind Akamai Bot Manager which blocks all
standard HTTP clients based on TLS fingerprinting.  curl_cffi impersonates
Chrome's BoringSSL TLS stack to bypass the WAF.

The download requires a form POST (Drupal form with CSRF token):
  1. GET the page → extract form_build_id + session cookie
  2. POST year/month → response HTML contains the file download link
  3. GET the file URL in the same session → CSV bytes

Sole proprietors are identified by license type combined with the
business name NOT matching a legal entity pattern (LLC/Inc/Corp/etc).
ATF publishes ~128K active licensees; roughly 20-30K are individuals.
"""

import io
import re
from calendar import monthrange
from datetime import date

import polars as pl
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

try:
    from curl_cffi import requests as cffi_req
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

_PAGE_URL = "https://www.atf.gov/firearms/tools-and-services-firearms-industry/federal-firearms-listings"
_FORM_ID_COMPLETE = "ffl-complete-export-form"

# License types that can be held by individuals (sole proprietors)
_INDIVIDUAL_LICENSE_TYPES = {"01", "02", "03", "06", "07", "08", "09", "10", "11"}

# Patterns indicating a business entity (not an individual)
_ENTITY_PATTERNS = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|co\.|ltd|limited|"
    r"lp|llp|pllc|trust|foundation|church|association|assoc|group|holdings|"
    r"enterprises|services|solutions|industries|international|company)\b",
    re.IGNORECASE,
)

_COLUMNS = [
    "lic_regn", "lic_dist", "lic_cnty", "lic_type", "lic_xprdte",
    "lic_seqn", "licensee_name", "business_name", "premise_street",
    "premise_city", "premise_state", "premise_zip_code", "mail_street",
    "mail_city", "mail_state", "mail_zip_code", "voice_phone",
]


def _is_retryable(exc: BaseException) -> bool:
    try:
        from curl_cffi.requests.exceptions import RequestsError
        if isinstance(exc, RequestsError):
            return True
    except ImportError:
        pass
    return False


class AtfFflScraper(BaseScraper):
    source_id = "atf_ffl"
    schedule = "0 4 1 * *"   # 1st of each month at 4 AM
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        if not _CFFI_AVAILABLE:
            raise RuntimeError(
                "curl_cffi is required for ATF FFL scraping. "
                "Install with: pip install curl_cffi"
            )

        last_period = state.get("cursor")
        today = date.today()

        # ATF publishes ~1 month behind. Walk back from last month up to 4 months.
        for months_back in range(1, 5):
            candidate = _month_offset(today, -months_back)
            period = candidate.strftime("%Y-%m")

            if period == last_period:
                return []  # already have the most recent available

            yy = str(candidate.year)[2:]   # e.g. "26"
            mm = f"{candidate.month:02d}"  # e.g. "03"

            # Probe whether this month is available by doing the full POST.
            # If the POST response contains a file URL, this period is live.
            try:
                file_url = _get_file_url(yy, mm)
            except Exception:
                continue

            if file_url:
                mmyy = f"{mm}{yy}"
                return [
                    DownloadTarget(
                        url=file_url,
                        source_id=self.source_id,
                        period=period,
                        doc_type=self.doc_type,
                        filename=f"atf_ffl_{period}.csv",
                        metadata={"cursor_after": period, "yy": yy, "mm": mm},
                    )
                ]

        return []

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=5, max=60),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def fetch(self, target: DownloadTarget) -> bytes:
        """Download the CSV using a curl_cffi session (bypasses Akamai WAF)."""
        yy = target.metadata.get("yy")
        mm = target.metadata.get("mm")
        if not (yy and mm):
            # Parse from URL filename as fallback
            filename = target.url.split("/")[-1]  # e.g. "0326-ffl-list.csv"
            mm, yy = filename[:2], filename[2:4]

        return _fetch_csv(yy, mm)

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")

        df = pl.read_csv(
            io.StringIO(text),
            has_header=True,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
        )

        # Normalize column names
        df = df.rename({c: c.strip().lower().replace(" ", "_") for c in df.columns})

        # Map by position if header is missing/malformed
        if "lic_type" not in df.columns and len(df.columns) >= 4:
            rename_map = {df.columns[i]: _COLUMNS[i]
                         for i in range(min(len(_COLUMNS), len(df.columns)))}
            df = df.rename(rename_map)

        records: list[ParsedRecord] = []

        for row in df.iter_rows(named=True):
            lic_type = (row.get("lic_type") or "").strip()
            business_name = (row.get("business_name") or row.get("licensee_name") or "").strip()
            licensee_name = (row.get("licensee_name") or "").strip()

            if not business_name and not licensee_name:
                continue

            name = business_name or licensee_name

            if lic_type not in _INDIVIDUAL_LICENSE_TYPES:
                continue

            if _ENTITY_PATTERNS.search(name):
                continue

            seqn = (row.get("lic_seqn") or "").strip()
            regn = (row.get("lic_regn") or "").strip()
            dist = (row.get("lic_dist") or "").strip()
            state_abbr = (row.get("premise_state") or "").strip()
            city = (row.get("premise_city") or "").strip()

            lic_number = f"{regn}-{dist}-{lic_type}-{seqn}" if regn else seqn

            records.append(
                ParsedRecord(
                    individuals=[
                        ParsedIndividual(
                            name=name,
                            relationship="ffl_licensee",
                            excerpt=f"ATF FFL {lic_number} in {city}, {state_abbr}",
                            identifiers={
                                "lic_number": lic_number,
                                "lic_type": lic_type,
                                "state": state_abbr,
                                "city": city,
                            },
                        )
                    ],
                    source_identifier=lic_number,
                )
            )

        return records


# ---------------------------------------------------------------------------
# curl_cffi helpers (session-based Akamai bypass)
# ---------------------------------------------------------------------------

def _make_session() -> "cffi_req.Session":
    return cffi_req.Session(impersonate="chrome124")


def _get_form_build_id(session: "cffi_req.Session") -> str:
    """GET the ATF FFL page and extract the Drupal CSRF form_build_id."""
    resp = session.get(_PAGE_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form", id=_FORM_ID_COMPLETE)
    if not form:
        raise RuntimeError("Could not find ffl-complete-export-form on ATF page")
    return form.find("input", {"name": "form_build_id"})["value"]


def _get_file_url(yy: str, mm: str) -> str | None:
    """POST the form and extract the resulting file URL from the response."""
    session = _make_session()
    form_build_id = _get_form_build_id(session)

    data = {
        "year": yy,
        "month": mm,
        "form_build_id": form_build_id,
        "form_id": _FORM_ID_COMPLETE,
        "op": "Apply",
    }
    resp = session.post(_PAGE_URL, data=data, timeout=60)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ffl-list" in href and href.endswith(".csv"):
            # Make absolute
            if href.startswith("/"):
                href = "https://www.atf.gov" + href
            return href
    return None


def _fetch_csv(yy: str, mm: str) -> bytes:
    """Full GET→POST→download flow; returns raw CSV bytes."""
    session = _make_session()
    form_build_id = _get_form_build_id(session)

    data = {
        "year": yy,
        "month": mm,
        "form_build_id": form_build_id,
        "form_id": _FORM_ID_COMPLETE,
        "op": "Apply",
    }
    post_resp = session.post(_PAGE_URL, data=data, timeout=60)
    post_resp.raise_for_status()

    soup = BeautifulSoup(post_resp.text, "html.parser")
    file_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "ffl-list" in href and href.endswith(".csv"):
            file_url = href if href.startswith("http") else "https://www.atf.gov" + href
            break

    if not file_url:
        raise RuntimeError(f"No FFL file URL found in POST response for {mm}/{yy}")

    dl = session.get(file_url, timeout=120, headers={"Referer": _PAGE_URL})
    dl.raise_for_status()
    return dl.content


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _month_offset(d: date, months: int) -> date:
    month = d.month + months
    year = d.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)
