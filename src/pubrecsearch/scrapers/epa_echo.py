"""EPA ECHO (Enforcement and Compliance History Online) scraper.

Source: https://echodata.epa.gov/echo/case_rest_services
Format: JSON REST API — case list + per-case detail
Update frequency: Varies (enforcement case activity logged as it occurs)
Schedule: Weekly (0 6 * * 3) — Wednesday morning
Relationship type: epa_enforcement_defendant

Only individual defendants (defendant_type = "I") are extracted.

Ongoing flow:
  discover()  returns a single target representing all new individual-defendant
              cases with activity since the cursor date.
  fetch()     paginates through get_cases, then fetches get_case_info for each
              case; returns the combined data as JSON bytes.
  parse()     extracts individual defendants from case detail records.

Historical bootstrap: scripts/bootstrap/epa_bootstrap.py works year by year.

Rate limit: max 2 req/sec — enforced via 0.5 s delay between requests.
"""

import json
import time
from datetime import date, timedelta
from typing import Any

from tenacity import retry, stop_after_attempt, wait_exponential

from ..base_scraper import BaseScraper
from ..http_client import make_client
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

_BASE_URL = "https://echodata.epa.gov/echo"
_CASES_URL = f"{_BASE_URL}/case_rest_services.get_cases"
_CASE_INFO_URL = f"{_BASE_URL}/case_rest_services.get_case_info"
_HEADERS = {
    "User-Agent": "PubRecSearch/1.0 (research@example.com)",
    "Accept": "application/json",
}
_REQ_DELAY = 0.5        # seconds between requests (2 req/sec max)
_CASE_PAGE_SIZE = 100   # cases per page in get_cases response


class EpaEchoScraper(BaseScraper):
    source_id = "epa_echo"
    schedule = "0 6 * * 3"  # weekly Wednesday at 6 AM
    doc_type = "json"

    def discover(self, state: dict) -> list[DownloadTarget]:
        cursor = state.get("cursor")
        # Default: look back 90 days on first run
        if cursor:
            since = cursor
        else:
            since = (date.today() - timedelta(days=90)).isoformat()

        today = date.today().isoformat()
        return [
            DownloadTarget(
                url=_CASES_URL,
                source_id=self.source_id,
                period=today,
                doc_type="json",
                filename=f"epa_echo_{today}.json",
                metadata={"since_date": since, "cursor_after": today},
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=5, max=60), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        """Fetch all individual-defendant cases since cursor; return combined JSON bytes."""
        since_iso = target.metadata.get("since_date", "")
        since_echo = _iso_to_echo_date(since_iso) if since_iso else "01/01/2015"

        case_ids = _fetch_case_ids_since(since_echo)
        if not case_ids:
            return json.dumps([]).encode()

        cases: list[dict] = []
        for case_id in case_ids:
            try:
                detail = _fetch_case_detail(case_id)
                if detail:
                    cases.append(detail)
            except Exception:
                pass  # log in runner; don't abort entire batch
            time.sleep(_REQ_DELAY)

        return json.dumps(cases).encode()

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        cases = json.loads(raw)
        return parse_cases(cases)


# ---------------------------------------------------------------------------
# Public helpers (used by bootstrap script)
# ---------------------------------------------------------------------------


def fetch_case_ids_for_year(year: int) -> list[str]:
    """Return all individual-defendant case ActivityIds active in a given year."""
    begin = f"01/01/{year}"
    end = f"12/31/{year}"
    return _fetch_case_ids(begin, end)


def parse_cases(cases: list[dict]) -> list[ParsedRecord]:
    """Extract ParsedRecords from a list of case detail dicts."""
    records: list[ParsedRecord] = []
    for case in cases:
        rec = _parse_single_case(case)
        if rec:
            records.append(rec)
    return records


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _iso_to_echo_date(iso: str) -> str:
    """Convert ISO date (YYYY-MM-DD) to ECHO API format (MM/DD/YYYY)."""
    parts = iso[:10].split("-")
    if len(parts) == 3:
        return f"{parts[1]}/{parts[2]}/{parts[0]}"
    return iso


def _fetch_case_ids_since(since_echo_date: str) -> list[str]:
    """Return ActivityIds for individual-defendant cases with activity since the given date."""
    return _fetch_case_ids(since_echo_date, None)


def _fetch_case_ids(begin_date: str, end_date: str | None) -> list[str]:
    """Paginate get_cases; return all ActivityIds for individual-defendant cases."""
    ids: list[str] = []
    page = 0

    with make_client(timeout=60, headers=_HEADERS) as client:
        while True:
            params: dict[str, Any] = {
                "output": "JSON",
                "p_defendant_type": "I",
                "p_case_activity_date_begin": begin_date,
                "p_page_no": str(page),
            }
            if end_date:
                params["p_case_activity_date_end"] = end_date

            resp = client.get(_CASES_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

            case_list = _nested_get(data, "Results.CaseList", "CaseList", "results")
            if not case_list:
                break

            for case in case_list:
                activity_id = (
                    _str_field(case, "ActivityId", "activity_id", "Id", "id")
                )
                if activity_id:
                    ids.append(activity_id)

            # Check for more pages
            total_str = _str_field(
                _nested_get(data, "Results", data) or {},
                "TotalCaseCount", "total", "count",
            )
            total = int(total_str) if total_str and total_str.isdigit() else 0
            if total == 0 or len(ids) >= total or len(case_list) < _CASE_PAGE_SIZE:
                break

            page += 1
            time.sleep(_REQ_DELAY)

    return ids


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
def _fetch_case_detail(activity_id: str) -> dict | None:
    """Fetch detailed case information for one ActivityId."""
    with make_client(timeout=60, headers=_HEADERS) as client:
        params = {"output": "JSON", "p_id": activity_id}
        resp = client.get(_CASE_INFO_URL, params=params)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Case parsing
# ---------------------------------------------------------------------------


def _parse_single_case(data: dict) -> ParsedRecord | None:
    """Extract individual defendants from one get_case_info response dict."""
    results = _nested_get(data, "Results", data) or data

    # Extract case metadata
    case_info = _nested_get(results, "CaseActivityData", "CaseEnforcementInfo", "case") or {}
    case_number = _str_field(case_info, "CaseNumber", "case_number", "CaseNum") or ""
    case_name = _str_field(case_info, "CaseName", "case_name") or ""
    activity_id = _str_field(case_info, "ActivityId", "activity_id") or ""
    program = _str_field(case_info, "ProgramCode", "program_code", "Program") or ""
    activity_date = _str_field(case_info, "ActivityDate", "activity_date", "DocketedDate") or ""
    state = _str_field(case_info, "StateCode", "state_code", "State") or ""

    # Extract penalty amounts
    penalties = _nested_get(results, "CasePenaltyData", "Penalties", "penalties") or []
    total_penalty = 0.0
    for pen in penalties:
        amt_str = _str_field(pen, "PenaltyAmount", "penalty_amount", "Amount") or ""
        try:
            total_penalty += float(amt_str.replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            pass

    # Locate defendant list
    defendants = (
        _nested_get(results, "CaseDefendantData", "CaseDefendants", "defendants",
                    "Defendants", "DefendantList")
        or []
    )

    individuals: list[ParsedIndividual] = []
    for def_rec in defendants:
        # Filter to individual defendants only
        def_type = _str_field(def_rec, "DefendantType", "defendant_type", "Type") or ""
        if def_type.upper() not in ("I", "INDIVIDUAL", ""):
            continue

        name = _str_field(
            def_rec, "DefendantName", "defendant_name", "Name", "name"
        ) or ""
        name = name.strip()
        if not name or not normalize_name(name):
            continue

        def_state = _str_field(def_rec, "DefendantState", "defendant_state", "State") or state
        def_city = _str_field(def_rec, "DefendantCity", "defendant_city", "City") or ""

        identifiers: dict = {}
        if case_number:
            identifiers["case_number"] = case_number
        if activity_id:
            identifiers["activity_id"] = activity_id
        if program:
            identifiers["program"] = program
        if def_state:
            identifiers["state"] = def_state
        if def_city:
            identifiers["city"] = def_city
        if activity_date:
            identifiers["activity_date"] = activity_date[:10]
        if total_penalty:
            identifiers["total_penalty_usd"] = total_penalty

        excerpt_parts = [f"EPA ECHO enforcement"]
        if program:
            excerpt_parts.append(program)
        if case_name:
            excerpt_parts.append(case_name[:80])
        if total_penalty:
            excerpt_parts.append(f"${total_penalty:,.0f} penalty")

        individuals.append(
            ParsedIndividual(
                name=name,
                relationship="epa_enforcement_defendant",
                excerpt=" | ".join(excerpt_parts),
                identifiers=identifiers,
            )
        )

    if not individuals:
        return None

    return ParsedRecord(
        individuals=individuals,
        source_identifier=case_number or activity_id,
    )


# ---------------------------------------------------------------------------
# Generic dict helpers for defensive API field access
# ---------------------------------------------------------------------------


def _nested_get(data: dict, *paths: str) -> Any:
    """Try multiple dot-separated key paths in a nested dict; return first match."""
    for path in paths:
        obj: Any = data
        for key in path.split("."):
            if not isinstance(obj, dict):
                obj = None
                break
            obj = obj.get(key)
        if obj is not None:
            return obj
    return None


def _str_field(d: dict, *keys: str) -> str | None:
    """Return the first non-empty string value found for any of the given keys."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        val = d.get(key)
        if val is not None:
            return str(val).strip() or None
    return None
