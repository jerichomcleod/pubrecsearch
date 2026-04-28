"""SAM.gov Exclusions scraper.

Source: https://api.sam.gov/entity-information/v4/exclusions
Format: Async CSV extract (format=csv parameter)
Auth: Free SAM.gov API key (register at sam.gov/profile/details)
Rate limit: 10 requests/day on public free tier
Update frequency: Daily
Relationship type: sam_excluded_individual

## API Rate Limit Reality

The SAM.gov v4 exclusions API has a hard limit of 10 requests/day for
public/personal API keys (no role assignment). The paginated JSON endpoint
maxes out at 10 records/page — meaning 100 records/day maximum. That's
unusable for a dataset of ~10K+ records.

## Extract API (this implementation)

The correct approach is the async CSV extract:
  1. GET /exclusions?api_key=KEY&format=csv
     → Response body contains a token URL:
       "Extract File will be available for download with url:
        https://api.sam.gov/entity-information/v4/download-exclusions?api_key=KEY&token=TOKEN"
  2. Poll the token URL until the file is ready (large files take seconds–minutes)
  3. Download the CSV (up to 1,000,000 records in one shot)

This uses only 2 API calls per run — well within the 10/day limit.
Individual filtering happens in parse() via the Classification column.
"""

import gzip
import io
import re
import time
from datetime import date

import httpx
import polars as pl
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..config import get_settings
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

_BASE_URL = "https://api.sam.gov/entity-information/v4/exclusions"
_DOWNLOAD_URL = "https://api.sam.gov/entity-information/v4/download-exclusions"

# Regex to pull the token URL from the extract response text
_TOKEN_URL_RE = re.compile(
    r"https://api\.sam\.gov/entity-information/v4/download-exclusions\?[^\s\"'<>]+"
)

_MAX_POLL_SECONDS = 300   # 5 minutes


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        # 429 is intentionally excluded: SAM.gov is rate-limited to 10 requests/day.
        # Retrying on 429 burns the remaining daily budget without any chance of success.
        # The scheduler will retry tomorrow when the quota resets.
        return exc.response.status_code in (500, 502, 503, 504)
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


class SamExclusionsScraper(BaseScraper):
    source_id = "sam_exclusions"
    schedule = "0 5 * * *"   # daily at 5 AM
    doc_type = "csv"

    def discover(self, state: dict) -> list[DownloadTarget]:
        today = date.today().isoformat()
        # Skip if we already imported today's extract (rate limit is 10 calls/day)
        if state.get("cursor") == today:
            return []
        return [
            DownloadTarget(
                url=_BASE_URL,
                source_id=self.source_id,
                period=today,
                doc_type=self.doc_type,
                filename=f"sam_exclusions_{today}.csv",
                metadata={"cursor_after": today},
            )
        ]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=10, max=120),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    def fetch(self, target: DownloadTarget) -> bytes:
        """Request a CSV extract, poll until ready, return the CSV bytes."""
        settings = get_settings()
        api_key = settings.sam_api_key
        if not api_key:
            raise RuntimeError("SAM_API_KEY is not configured")

        # Step 1: request the extract — response body contains the token URL
        with make_client(timeout=60) as client:
            resp = client.get(_BASE_URL, params={"api_key": api_key, "format": "csv"})
            resp.raise_for_status()
            body = resp.text

        # Step 2: parse the token download URL out of the response
        match = _TOKEN_URL_RE.search(body)
        if not match:
            raise RuntimeError(
                f"Could not find download token URL in SAM.gov extract response: {body[:500]}"
            )
        token_url = match.group(0)
        # Ensure the API key is embedded (SAM docs show REPLACE_WITH_API_KEY placeholder)
        token_url = token_url.replace("REPLACE_WITH_API_KEY", api_key)
        if "api_key" not in token_url:
            sep = "&" if "?" in token_url else "?"
            token_url = f"{token_url}{sep}api_key={api_key}"

        # Step 3: poll the token URL until the file is ready
        csv_bytes = self._poll_and_download(token_url)
        return csv_bytes

    def _poll_and_download(self, token_url: str) -> bytes:
        """Poll the token URL, retrying on 404 (not ready yet), return file bytes."""
        deadline = time.time() + _MAX_POLL_SECONDS
        attempt = 0

        while time.time() < deadline:
            attempt += 1
            with make_client(timeout=120) as client:
                resp = client.get(token_url)

            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                # If it's JSON, the file isn't ready yet (SAM returns a status message)
                if "application/json" in content_type or resp.content.startswith(b"{"):
                    pass  # fall through to sleep
                else:
                    return resp.content

            elif resp.status_code == 404:
                pass  # file not ready yet

            elif resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(
                    f"SAM download error {resp.status_code}",
                    request=resp.request,
                    response=resp,
                )

            wait = min(10 * attempt, 60)
            time.sleep(wait)

        raise TimeoutError(
            f"SAM.gov extract file not ready after {_MAX_POLL_SECONDS}s ({attempt} polls)"
        )

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Parse the CSV extract, filter to individuals."""
        # SAM.gov returns the extract as a raw gzip file (not via Content-Encoding),
        # so httpx does not auto-decompress it. Detect and unwrap before decoding.
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)

        try:
            text = raw.decode("utf-8-sig")   # handle BOM
        except UnicodeDecodeError:
            text = raw.decode("latin-1")

        df = pl.read_csv(
            io.StringIO(text),
            has_header=True,
            infer_schema_length=0,
            ignore_errors=True,
            truncate_ragged_lines=True,
        )

        # Normalize column names to lowercase with underscores
        df = df.rename({c: c.strip().lower().replace(" ", "_") for c in df.columns})

        records: list[ParsedRecord] = []

        for row in df.iter_rows(named=True):
            classification = (row.get("classification") or "").strip().lower()
            if "individual" not in classification:
                continue

            first = (row.get("first") or row.get("first_name") or "").strip()
            last = (row.get("last") or row.get("last_name") or "").strip()
            mid = (row.get("middle") or row.get("middle_name") or "").strip()

            if not last and not first:
                name = (row.get("name") or row.get("exclusion_name") or "").strip()
                if not name:
                    continue
            else:
                name = " ".join(p for p in [first, mid, last] if p)

            uei = (row.get("unique_entity_id") or row.get("uei_sam") or "").strip()
            cage = (row.get("cage_code") or "").strip()
            agency = (row.get("excluding_agency") or row.get("agency") or "").strip()
            excl_type = (row.get("exclusion_type") or row.get("type") or "").strip()
            active_date = (row.get("active_date") or row.get("activation_date") or "").strip()
            termination_date = (row.get("termination_date") or "").strip()

            identifiers: dict = {}
            if uei:
                identifiers["uei_sam"] = uei
            if cage:
                identifiers["cage"] = cage

            excerpt_parts = ["SAM excluded"]
            if agency:
                excerpt_parts.append(f"by {agency}")
            if excl_type:
                excerpt_parts.append(f"({excl_type})")
            if active_date:
                excerpt_parts.append(f"since {active_date}")
            if termination_date:
                excerpt_parts.append(f"until {termination_date}")

            records.append(
                ParsedRecord(
                    individuals=[
                        ParsedIndividual(
                            name=name,
                            relationship="sam_excluded_individual",
                            excerpt=" ".join(excerpt_parts),
                            identifiers=identifiers,
                        )
                    ],
                    source_identifier=uei or name,
                )
            )

        return records
