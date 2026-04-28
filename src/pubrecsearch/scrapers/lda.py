"""LDA Lobbying Disclosure Act scraper.

Source: https://lda.senate.gov/api/v1/
Format: Paginated JSON REST API
Update frequency: Quarterly (LD-2 reports due 20 days after quarter end); amendments filed continuously
Schedule: Weekly (0 6 * * 1) — catches amendments and late filings
Relationship types: lda_lobbyist, lda_registrant_contact

Requires LDA_API_KEY in .env (free registration at https://lda.senate.gov/api/register/).
Rate limit: 120 req/min with key.

discover() returns one target per run covering filings received since the cursor date.
fetch() paginates through the API (respecting 0.5 s/req) and returns accumulated JSON.
parse() extracts named lobbyists and registrant contacts from filing records.

Historical bootstrap: use scripts/bootstrap/lda_bootstrap.py (2015–present).
"""

import json
import time
from datetime import date, timedelta

from tenacity import retry, stop_after_attempt, wait_exponential

from ..base_scraper import BaseScraper
from ..config import get_settings
from ..db import bulk_insert_individuals
from ..http_client import make_client
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

_FILINGS_URL = "https://lda.senate.gov/api/v1/filings/"
_PAGE_SIZE = 25          # LDA default page size
_REQ_DELAY = 0.5         # seconds between requests (120 req/min = 0.5 s min)

# Markers that indicate an org name rather than a person
_ORG_MARKERS = frozenset([
    "llc", "inc", "corp", "l.p.", "lp", "llp", "ltd", "association",
    "group", "firm", "company", "solutions", "services", "consulting",
    "strategies", "partners", "affairs", "lobbying", "government",
    "coalition", "council", "alliance", "institute", "foundation",
    "bureau", "chamber", "federation", "union", "league",
])


def _auth_headers() -> dict:
    settings = get_settings()
    return {
        "Authorization": f"Token {settings.lda_api_key}",
        "User-Agent": "PubRecSearch/1.0 (research@example.com)",
        "Accept": "application/json",
    }


class LdaScraper(BaseScraper):
    source_id = "lda"
    schedule = "0 6 * * 1"  # every Monday at 6 AM — catches amendments + late filings
    doc_type = "json"

    def discover(self, state: dict) -> list[DownloadTarget]:
        cursor_dt = state.get("cursor")
        # Default: 90 days back on first run to catch the current quarter
        since = cursor_dt or (date.today() - timedelta(days=90)).isoformat()
        today = date.today().isoformat()
        return [
            DownloadTarget(
                url=_FILINGS_URL,
                source_id=self.source_id,
                period=today,
                doc_type="json",
                filename=f"lda_filings_{today}.json",
                metadata={"received_dt__gte": since, "cursor_after": today},
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        """Paginate through the LDA API and return all results as JSON bytes."""
        headers = _auth_headers()
        since = target.metadata.get("received_dt__gte", "")

        all_results: list[dict] = []
        # First request uses query params; subsequent requests use the full 'next' URL
        # returned by the API (which already encodes all filter params).
        next_url: str | None = _FILINGS_URL
        first = True

        with make_client(timeout=60, headers=headers) as client:
            while next_url:
                if first:
                    params = {"received_dt__gte": since, "page_size": _PAGE_SIZE}
                    resp = client.get(next_url, params=params)
                    resp.raise_for_status()
                    first = False
                else:
                    resp = client.get(next_url)
                    # The LDA API occasionally returns 400 on deeper paginated pages
                    # when using date-range filters. Treat this as end-of-results rather
                    # than a hard failure so we keep whatever we've already collected.
                    if resp.status_code == 400:
                        break
                    resp.raise_for_status()

                data = resp.json()
                all_results.extend(data.get("results") or [])
                next_url = data.get("next")  # None on last page
                if next_url:
                    time.sleep(_REQ_DELAY)

        return json.dumps(all_results).encode()

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        filings = json.loads(raw)
        return parse_filings(filings)

    def parse_to_db(self, raw: bytes | None, target: DownloadTarget, doc_id: int) -> tuple[int, int]:
        """Bulk-insert LDA lobbyists using PostgreSQL COPY."""
        assert raw is not None
        filings = json.loads(raw)
        rows = filings_to_rows(filings)
        if not rows:
            return 0, 0
        from ..db import get_conn
        with get_conn() as conn:
            return bulk_insert_individuals(conn, doc_id, "lda_lobbyist", rows)


# ---------------------------------------------------------------------------
# Public helpers used by the bootstrap script
# ---------------------------------------------------------------------------


def fetch_filings_page(
    client,
    url: str,
    params: dict | None = None,
) -> tuple[list[dict], str | None]:
    """Fetch one API page; return (results, next_url)."""
    resp = client.get(url, params=params)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results") or [], data.get("next")


def fetch_year_filings(year: int) -> list[dict]:
    """Download ALL filings for a given year (used by bootstrap)."""
    headers = _auth_headers()
    all_results: list[dict] = []
    next_url: str | None = _FILINGS_URL
    first = True

    with make_client(timeout=120, headers=headers) as client:
        while next_url:
            if first:
                params = {"filing_year": year, "page_size": _PAGE_SIZE}
                results, next_url = fetch_filings_page(client, next_url, params)
                first = False
            else:
                results, next_url = fetch_filings_page(client, next_url)
            all_results.extend(results)
            if next_url:
                time.sleep(_REQ_DELAY)

    return all_results


def parse_filings(filings: list[dict]) -> list[ParsedRecord]:
    """Extract ParsedRecords from a list of LDA filing dicts."""
    records: list[ParsedRecord] = []
    for filing in filings:
        individuals = _extract_individuals(filing)
        if individuals:
            records.append(
                ParsedRecord(
                    individuals=individuals,
                    source_identifier=filing.get("filing_uuid"),
                )
            )
    return records


def filings_to_rows(filings: list[dict]) -> list[dict]:
    """Convert filing dicts to row dicts for bulk_insert_individuals."""
    rows: list[dict] = []
    for filing in filings:
        for ind in _extract_individuals(filing):
            name_norm = normalize_name(ind.name)
            if not name_norm:
                continue
            rows.append({
                "name": ind.name,
                "name_norm": name_norm,
                "excerpt": ind.excerpt or "",
                "identifiers": ind.identifiers or {},
            })
    return rows


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _extract_individuals(filing: dict) -> list[ParsedIndividual]:
    filing_uuid = filing.get("filing_uuid") or ""
    filing_type = filing.get("filing_type_display") or filing.get("filing_type") or ""
    filing_period = filing.get("filing_period_display") or filing.get("filing_period") or ""
    filing_year = str(filing.get("filing_year") or "")
    registrant = filing.get("registrant") or {}
    registrant_name = (registrant.get("name") or "").strip()
    registrant_id = str(registrant.get("id") or "")
    income = str(filing.get("income") or "")
    expenses = str(filing.get("expenses") or "")

    base_ids: dict = {
        "filing_uuid": filing_uuid,
        "filing_type": filing_type,
        "filing_period": filing_period,
        "filing_year": filing_year,
        "registrant_id": registrant_id,
        "registrant_name": registrant_name,
    }
    if income:
        base_ids["income"] = income
    if expenses:
        base_ids["expenses"] = expenses

    period_label = f"{filing_period} {filing_year}".strip()
    individuals: list[ParsedIndividual] = []

    # ---- Lobbyists listed on the filing ----
    for lob in filing.get("lobbyists") or []:
        first = (lob.get("lobbyist_first_name") or "").strip()
        last = (lob.get("lobbyist_last_name") or "").strip()
        suffix = (lob.get("lobbyist_suffix") or "").strip()
        name = " ".join(p for p in [first, last, suffix] if p)
        if not name or not normalize_name(name):
            continue

        idents = dict(base_ids)
        # Covered government entities this lobbyist contacted
        cge = [
            (g.get("name") or "").strip()
            for g in (lob.get("covered_government_entities") or [])
            if g.get("name")
        ]
        if cge:
            idents["covered_govt_entities"] = cge

        # Activity codes / general issue codes
        activity_codes = [
            (a.get("general_issue_code_display") or "")
            for a in (filing.get("lobbying_activities") or [])
            if a.get("general_issue_code_display")
        ]
        if activity_codes:
            idents["issue_areas"] = list(dict.fromkeys(activity_codes))  # deduplicated

        individuals.append(
            ParsedIndividual(
                name=name,
                relationship="lda_lobbyist",
                excerpt=f"{period_label} | {registrant_name} | {filing_type}",
                identifiers=idents,
            )
        )

    # ---- Registrant contact name (if it looks like a person) ----
    contact_name = (registrant.get("contact_name") or "").strip()
    if contact_name and _looks_like_person(contact_name) and normalize_name(contact_name):
        idents = dict(base_ids)
        idents["role"] = "registrant_contact"
        individuals.append(
            ParsedIndividual(
                name=contact_name,
                relationship="lda_registrant_contact",
                excerpt=f"{period_label} | {registrant_name} | Contact",
                identifiers=idents,
            )
        )

    return individuals


def _looks_like_person(name: str) -> bool:
    """Heuristic: True if the name looks like a person rather than an organization."""
    lower = name.lower()
    return not any(marker in lower for marker in _ORG_MARKERS)
