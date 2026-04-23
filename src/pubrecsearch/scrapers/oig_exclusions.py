"""OIG LEIE (List of Excluded Individuals/Entities) scraper.

Source: https://oig.hhs.gov/exclusions/exclusions_list.asp
Format: Monthly full CSV replacement (~79K rows, ~30 MB)
Update frequency: Monthly
Relationship type: oig_excluded_individual

Only TYPE=Individual rows are imported; entity rows are skipped.
"""

import io
from datetime import date

import httpx
import polars as pl
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

# OIG publishes the updated file at a fixed URL
_CSV_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"


class OigExclusionsScraper(BaseScraper):
    source_id = "oig_exclusions"
    schedule = "0 3 8 * *"   # 8th of each month at 3 AM (OIG updates early in the month)
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        today = date.today()
        period = today.strftime("%Y-%m")
        return [
            DownloadTarget(
                url=_CSV_URL,
                source_id=self.source_id,
                period=period,
                doc_type=self.doc_type,
                filename=f"oig_leie_{period}.csv",
                metadata={"cursor_after": period},
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=120) as client:
            resp = client.get(target.url, headers=target.headers)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        df = pl.read_csv(
            io.StringIO(text),
            has_header=True,
            infer_schema_length=0,
            ignore_errors=True,
        )

        # Normalize column names to lowercase with underscores
        df = df.rename({c: c.strip().upper() for c in df.columns})

        records: list[ParsedRecord] = []

        for row in df.iter_rows(named=True):
            # Individuals have LASTNAME populated; entities have BUSNAME but no LASTNAME.
            # EXCLTYPE is a statutory code (e.g. "1128a1"), not a text label.
            last = (row.get("LASTNAME") or "").strip()
            if not last:
                continue

            first = (row.get("FIRSTNAME") or "").strip()
            mid = (row.get("MIDNAME") or "").strip()

            parts = [first, mid, last]
            name = " ".join(p for p in parts if p)

            npi = (row.get("NPI") or "").strip()
            dob = (row.get("DOB") or "").strip()
            excl_date = (row.get("EXCDATE") or "").strip()   # OIG column is EXCDATE, not EXCLDATE
            reinstate_date = (row.get("REINDATE") or "").strip()
            waiver_date = (row.get("WAIVERDATE") or "").strip()
            specialty = (row.get("SPECIALTY") or "").strip()
            state = (row.get("STATE") or "").strip()

            identifiers: dict = {}
            if npi:
                identifiers["npi"] = npi
            if dob:
                identifiers["dob"] = dob
            if state:
                identifiers["state"] = state

            excerpt_parts = [f"OIG excluded {excl_date}"]
            if specialty:
                excerpt_parts.append(f"specialty: {specialty}")
            if reinstate_date:
                excerpt_parts.append(f"reinstated: {reinstate_date}")
            if waiver_date:
                excerpt_parts.append(f"waiver: {waiver_date}")

            records.append(
                ParsedRecord(
                    individuals=[
                        ParsedIndividual(
                            name=name,
                            relationship="oig_excluded_individual",
                            excerpt="; ".join(excerpt_parts),
                            identifiers=identifiers,
                        )
                    ],
                    source_identifier=npi or f"{last}_{first}_{dob}",
                )
            )

        return records
