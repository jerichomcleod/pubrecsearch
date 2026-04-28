"""FARA (Foreign Agents Registration Act) scraper.

Source: https://efile.fara.gov/bulk/zip/
Format: Daily bulk ZIP → CSV (ISO-8859-1 encoding)
Update frequency: Daily (full-replacement snapshots)
Schedule: Daily (0 4 * * *)
Relationship types: fara_foreign_agent, fara_short_form_filer

Two bulk files are processed per run:
  1. FARA_All_Registrants.csv.zip — all registrants (orgs + individuals)
     → filter RegistrantType = "Individual"; relationship = fara_foreign_agent
  2. FARA_All_Short_Form.csv.zip — NSD-6 short-form filers
     → individuals employed by registrant organizations; relationship = fara_short_form_filer

Both files are full daily snapshots. The runner deduplicates via SHA-256 hash —
if the file is unchanged from the last download, no re-parsing occurs.

Encoding note: FARA explicitly documents ISO-8859-1 (Latin-1). Polars uses
encoding="latin1" for this.
"""

import io
import zipfile
from datetime import date

import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..base_scraper import BaseScraper
from ..http_client import make_client
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

_REGISTRANTS_URL = "https://efile.fara.gov/bulk/zip/FARA_All_Registrants.csv.zip"
_SHORT_FORM_URL = "https://efile.fara.gov/bulk/zip/FARA_All_Short_Form.csv.zip"
_HEADERS = {"User-Agent": "PubRecSearch/1.0 (research@example.com)"}


class FaraScraper(BaseScraper):
    source_id = "fara"
    schedule = "0 4 * * *"  # daily at 4 AM
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        today = date.today().isoformat()
        # Return both ZIPs unconditionally; runner deduplicates via SHA-256.
        return [
            DownloadTarget(
                url=_REGISTRANTS_URL,
                source_id=self.source_id,
                period=today,
                doc_type="zip",
                filename=f"fara_registrants_{today}.zip",
                metadata={"file_type": "registrants"},
            ),
            DownloadTarget(
                url=_SHORT_FORM_URL,
                source_id=self.source_id,
                period=today,
                doc_type="zip",
                filename=f"fara_short_form_{today}.zip",
                metadata={"file_type": "short_form"},
            ),
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=120, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        file_type = target.metadata.get("file_type", "registrants")
        if file_type == "registrants":
            return _parse_registrants(raw)
        else:
            return _parse_short_form(raw)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _extract_csv(raw: bytes) -> bytes:
    """Extract the first CSV from a ZIP archive."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"No CSV in FARA ZIP. Contents: {zf.namelist()}")
        return zf.read(csv_names[0])


def _read_fara_csv(raw_csv: bytes) -> pl.DataFrame:
    """Parse a FARA CSV (ISO-8859-1, comma-delimited) into a DataFrame."""
    return pl.read_csv(
        io.BytesIO(raw_csv),
        infer_schema_length=0,
        ignore_errors=True,
        truncate_ragged_lines=True,
        encoding="latin1",
    )


def _col(df: pl.DataFrame, *candidates: str) -> str | None:
    """Return the first column name that exists in the DataFrame (case-insensitive)."""
    lower_map = {c.lower(): c for c in df.columns}
    for cand in candidates:
        found = lower_map.get(cand.lower())
        if found:
            return found
    return None


def _parse_registrants(raw: bytes) -> list[ParsedRecord]:
    """Extract individual registrants from FARA_All_Registrants.csv."""
    try:
        csv_bytes = _extract_csv(raw)
        df = _read_fara_csv(csv_bytes)
    except Exception as exc:
        raise ValueError(f"FARA registrants parse error: {exc}") from exc

    # Detect columns (FARA may change column names between releases)
    id_col = _col(df, "RegistrantID", "registrant_id", "Id")
    name_col = _col(df, "Name", "RegistrantName", "registrant_name")
    type_col = _col(df, "RegistrantType", "registrant_type", "Type")
    reg_date_col = _col(df, "RegistrationDate", "registration_date", "RegDate")
    term_date_col = _col(df, "TerminationDate", "termination_date", "TermDate")
    country_col = _col(df, "Country", "country_of_formation", "CountryOfFormation")
    address_col = _col(df, "Address", "address")
    city_col = _col(df, "City", "city")
    state_col = _col(df, "State", "state")

    if not name_col:
        return []

    # Filter to individual registrants only
    if type_col:
        df = df.filter(
            pl.col(type_col).str.to_lowercase().str.contains("individual")
        )

    records: list[ParsedRecord] = []
    for row in df.iter_rows(named=True):
        name = (row.get(name_col) or "").strip() if name_col else ""
        if not name or not normalize_name(name):
            continue

        registrant_id = str(row.get(id_col) or "").strip() if id_col else ""
        reg_date = str(row.get(reg_date_col) or "").strip() if reg_date_col else ""
        term_date = str(row.get(term_date_col) or "").strip() if term_date_col else ""
        country = (row.get(country_col) or "").strip() if country_col else ""
        address = (row.get(address_col) or "").strip() if address_col else ""
        city = (row.get(city_col) or "").strip() if city_col else ""
        state = (row.get(state_col) or "").strip() if state_col else ""

        identifiers: dict = {}
        if registrant_id:
            identifiers["registrant_id"] = registrant_id
        if country:
            identifiers["country"] = country
        if reg_date:
            identifiers["registration_date"] = reg_date
        if term_date:
            identifiers["termination_date"] = term_date
        if state:
            identifiers["state"] = state

        location_parts = [p for p in [city, state, country] if p]
        location = ", ".join(location_parts)
        status = "terminated" if term_date else "active"
        excerpt = f"FARA registrant | {status}" + (f" | {location}" if location else "")

        records.append(
            ParsedRecord(
                individuals=[
                    ParsedIndividual(
                        name=name,
                        relationship="fara_foreign_agent",
                        excerpt=excerpt,
                        identifiers=identifiers,
                    )
                ],
                source_identifier=registrant_id or name,
            )
        )

    return records


def _parse_short_form(raw: bytes) -> list[ParsedRecord]:
    """Extract individual filers from FARA_All_Short_Form.csv (NSD-6)."""
    try:
        csv_bytes = _extract_csv(raw)
        df = _read_fara_csv(csv_bytes)
    except Exception as exc:
        raise ValueError(f"FARA short form parse error: {exc}") from exc

    # Short form column names — FARA uses various naming conventions
    # Try split first/last name columns first, fall back to full name
    first_col = _col(df, "FirstName", "first_name", "FilerFirstName")
    last_col = _col(df, "LastName", "last_name", "FilerLastName")
    name_col = _col(df, "Name", "FilerName", "FullName", "filer_name")
    reg_id_col = _col(df, "RegistrantID", "registrant_id", "RegID")
    reg_name_col = _col(df, "RegistrantName", "registrant_name", "OrgName")
    date_col = _col(df, "DateStamp", "date_stamp", "RegistrationDate", "FilingDate")
    country_col = _col(df, "Country", "country", "ForeignPrincipalCountry")
    address_col = _col(df, "Address", "address")
    city_col = _col(df, "City", "city")
    state_col = _col(df, "State", "state")

    records: list[ParsedRecord] = []
    for row in df.iter_rows(named=True):
        # Build name from split columns or full name column
        if first_col and last_col:
            first = (row.get(first_col) or "").strip()
            last = (row.get(last_col) or "").strip()
            name = f"{first} {last}".strip() if first else last
        elif name_col:
            name = (row.get(name_col) or "").strip()
        else:
            continue

        if not name or not normalize_name(name):
            continue

        registrant_id = str(row.get(reg_id_col) or "").strip() if reg_id_col else ""
        registrant_name = (row.get(reg_name_col) or "").strip() if reg_name_col else ""
        filing_date = str(row.get(date_col) or "").strip() if date_col else ""
        country = (row.get(country_col) or "").strip() if country_col else ""
        address = (row.get(address_col) or "").strip() if address_col else ""
        city = (row.get(city_col) or "").strip() if city_col else ""
        state = (row.get(state_col) or "").strip() if state_col else ""

        identifiers: dict = {}
        if registrant_id:
            identifiers["registrant_id"] = registrant_id
        if registrant_name:
            identifiers["registrant_name"] = registrant_name
        if country:
            identifiers["country"] = country
        if filing_date:
            identifiers["filing_date"] = filing_date
        if state:
            identifiers["state"] = state

        location_parts = [p for p in [city, state, country] if p]
        location = ", ".join(location_parts)
        excerpt_parts = ["FARA short-form filer"]
        if registrant_name:
            excerpt_parts.append(f"for {registrant_name[:60]}")
        if location:
            excerpt_parts.append(location)
        excerpt = " | ".join(excerpt_parts)

        records.append(
            ParsedRecord(
                individuals=[
                    ParsedIndividual(
                        name=name,
                        relationship="fara_short_form_filer",
                        excerpt=excerpt,
                        identifiers=identifiers,
                    )
                ],
                source_identifier=registrant_id or name,
            )
        )

    return records
