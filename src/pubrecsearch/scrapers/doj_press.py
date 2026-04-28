"""DOJ Press Releases scraper.

Source: https://www.justice.gov/news/press-releases/rss.xml
Format: RSS feed → individual HTML press release pages
Update frequency: Continuous (multiple releases per day)
Schedule: Daily (0 7 * * *)
Relationship types: doj_defendant (high confidence), doj_subject (medium confidence)

Ongoing (automated): RSS polling for items newer than the cursor pubDate.
  discover() fetches the RSS, returns one DownloadTarget per new press release URL.
  fetch()    downloads the full HTML of one press release page.
  parse()    extracts named individuals via deterministic regex — no LLM.

Historical bootstrap: scripts/bootstrap/doj_bootstrap.py paginates the HTML listing.

Extraction confidence levels
  high:   Title patterns — "United States v. Name", "Name Sentenced/Charged/Convicted/..."
  medium: Body patterns  — "defendant Name", "Name, of City", "Name, age NN"

False-positive filtering removes common non-person capitalized phrases.
Raw HTML of each press release is stored in R2 for auditability.
"""

import re
import time
from datetime import date, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from ..base_scraper import BaseScraper
from ..http_client import make_client
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord
from ..normalize import normalize_name

_RSS_URL = "https://www.justice.gov/news/press-releases/rss.xml"
_LISTING_URL = "https://www.justice.gov/news/press-releases"
_HEADERS = {
    "User-Agent": "PubRecSearch/1.0 (research@example.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
}
_REQ_DELAY = 1.1  # seconds between requests (DOJ: max ~1 req/sec)

# ---------------------------------------------------------------------------
# Name extraction patterns
# ---------------------------------------------------------------------------

# First name: simple word only (no hyphens — prevents "Co-defendant" matching)
# Last name tokens: allow hyphens for hyphenated surnames (e.g. "Smith-Jones")
_NAME_RE = (
    r"[A-Z][a-z]+"                            # first name (lowercase after initial — no "Co-")
    r"(?:\s+[A-Z]\.?)?"                       # optional middle initial
    r"(?:\s+[A-Z][A-Za-z'-]+){1,2}"           # 1-2 last name tokens (hyphens OK)
    r"(?:\s+(?:Jr\.|Sr\.|II|III|IV))?"        # optional suffix
)

_TITLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "United States v. John Smith"  or  "U.S. v. Smith"
    (re.compile(rf"United States v\.\s+({_NAME_RE})", re.IGNORECASE), "high"),
    # Leading name + action verb: "John Smith Sentenced / Charged / Pleads / ..."
    (re.compile(
        rf"^({_NAME_RE})\s+"
        r"(?:Sentenced|Pleads\s+Guilty|Charged|Convicted|Indicted|Arrested|"
        r"Admits|Agrees\s+to|Ordered\s+to|Pays|Receives|Gets)"
    ), "high"),
    # "Sentencing of John Smith"
    (re.compile(rf"(?:Sentencing|Conviction|Indictment|Arrest)\s+of\s+({_NAME_RE})"), "high"),
]

_BODY_PATTERNS: list[tuple[re.Pattern, str]] = [
    # "defendant John Smith" / "defendants John Smith and ..."
    (re.compile(rf"\bdefendants?\s+({_NAME_RE})"), "medium"),
    # "John Smith, age 45," or "John Smith, 45,"
    (re.compile(rf"({_NAME_RE}),\s+(?:age\s+)?\d{{2}},"), "medium"),
    # "John Smith, of Chicago" / "John Smith, a resident of"
    (re.compile(rf"({_NAME_RE}),\s+(?:of\s+[A-Z]|a\s+resident\s+of\s+[A-Z])"), "medium"),
    # "John Smith was sentenced / charged / convicted"
    (re.compile(
        rf"({_NAME_RE})\s+was\s+(?:sentenced|charged|convicted|indicted|arrested|pleaded)"
    ), "medium"),
    # "sentence[d] John Smith to"
    (re.compile(rf"sentenced\s+({_NAME_RE})\s+to"), "medium"),
]

# Words that should not appear anywhere in a plausible person name
_NAME_STOPWORDS = frozenset([
    # Government / legal entities
    "united", "states", "federal", "national", "state", "county", "district",
    "circuit", "department", "division", "bureau", "office", "agency", "service",
    "court", "judge", "justice", "attorney", "general", "assistant",
    # Common legal document words (capitalized in titles)
    "complaint", "indictment", "information", "superseding", "conspiracy",
    "fraud", "theft", "wire", "mail", "drug", "enforcement",
    "securities", "exchange", "commission", "internal", "revenue",
    "immigration", "customs", "border", "protection",
    # Titles / honorifics that can precede a name
    "special", "senior", "chief", "deputy", "acting", "former", "director",
    # Generic words sometimes capitalized in press releases
    "press", "release", "statement", "update", "notice",
    # Number words (prevents "Two Men", "Three Defendants", etc.)
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "several", "multiple", "numerous",
    # Plural group nouns (prevents "Two Men", "Co-defendants", etc.)
    "men", "women", "people", "persons", "individuals", "defendants",
    "officers", "agents", "officials", "employees", "members",
    # Role prefixes that can appear hyphenated (co-defendant, etc.)
    "co", "ex", "non",
])


class DojPressScraper(BaseScraper):
    source_id = "doj_press"
    schedule = "0 7 * * *"  # daily at 7 AM
    doc_type = "html"

    def discover(self, state: dict) -> list[DownloadTarget]:
        cursor_dt = state.get("cursor")  # ISO datetime string or None
        try:
            raw = _fetch_url(_RSS_URL, timeout=30)
        except Exception:
            return []

        items = _parse_rss(raw)
        targets = []
        for item in items:
            pub_dt = item.get("pubDate_dt")
            if not pub_dt:
                continue
            pub_iso = pub_dt.isoformat()
            if cursor_dt and pub_iso <= cursor_dt:
                continue

            url = item["link"]
            slug = url.rstrip("/").split("/")[-1]
            pub_date = pub_dt.date().isoformat()
            targets.append(
                DownloadTarget(
                    url=url,
                    source_id=self.source_id,
                    period=pub_date,
                    doc_type="html",
                    filename=f"doj_{slug}.html",
                    metadata={
                        "title": item.get("title", ""),
                        "published": pub_iso,
                        "description": item.get("description", ""),
                        "cursor_after": pub_iso,
                    },
                )
            )

        return targets

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=60, headers=_HEADERS) as client:
            resp = client.get(target.url)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        title = target.metadata.get("title", "")
        pub_date = target.metadata.get("published", "")[:10]
        pr_url = target.url

        # Extract body text from HTML if available; fall back to RSS description
        body_text = ""
        if raw:
            body_text = _extract_body_text(raw)

        # Prefer the full page title if we have HTML
        if raw:
            html_title = _extract_title(raw)
            if html_title:
                title = html_title

        individuals = extract_individuals(title, body_text, pr_url, pub_date)
        if not individuals:
            return []

        excerpt = title + (" | " + body_text[:500] if body_text else "")
        return [
            ParsedRecord(
                individuals=individuals,
                source_identifier=pr_url,
            )
        ]


# ---------------------------------------------------------------------------
# Public helpers (used by bootstrap script)
# ---------------------------------------------------------------------------


def extract_individuals(
    title: str,
    body_text: str,
    pr_url: str,
    pub_date: str,
) -> list[ParsedIndividual]:
    """Apply regex extraction to title + body; return deduplicated ParsedIndividuals."""
    seen: dict[str, ParsedIndividual] = {}  # norm_name → best individual

    base_ids = {"url": pr_url, "published": pub_date}

    for pattern, confidence in _TITLE_PATTERNS:
        for m in pattern.finditer(title):
            name = m.group(1).strip()
            if _is_plausible_name(name):
                _add_individual(seen, name, "doj_defendant", confidence, title, base_ids)

    for pattern, confidence in _BODY_PATTERNS:
        for m in pattern.finditer(body_text[:3000]):  # first ~3000 chars of body
            name = m.group(1).strip()
            if _is_plausible_name(name):
                rel = "doj_defendant" if confidence == "high" else "doj_subject"
                _add_individual(seen, name, rel, confidence, title, base_ids)

    return list(seen.values())


def fetch_listing_page(page: int) -> list[dict]:
    """Fetch one page of the DOJ press release listing; return list of {url, title, date} dicts."""
    url = f"{_LISTING_URL}?page={page}"
    try:
        raw = _fetch_url(url, timeout=30)
        return _parse_listing_html(raw)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_url(url: str, timeout: int = 60) -> bytes:
    with make_client(timeout=timeout, headers=_HEADERS) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def _parse_rss(raw: bytes) -> list[dict]:
    """Parse RSS bytes into a list of item dicts."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []

    items = []
    for item in root.iter("item"):
        def t(tag: str) -> str:
            el = item.find(tag)
            return (el.text or "").strip() if el is not None else ""

        pub_str = t("pubDate")
        pub_dt = None
        if pub_str:
            try:
                pub_dt = parsedate_to_datetime(pub_str)
                if pub_dt.tzinfo is None:
                    pub_dt = pub_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        items.append({
            "title": t("title"),
            "link": t("link") or t("guid"),
            "description": t("description"),
            "pubDate": pub_str,
            "pubDate_dt": pub_dt,
        })

    return items


def _parse_listing_html(raw: bytes) -> list[dict]:
    """Parse a DOJ listing page; return [{url, title, date_str}] for each PR link."""
    soup = BeautifulSoup(raw, "lxml")
    results = []

    # DOJ listing uses various markup; try common selectors
    for el in soup.select("li.views-row, div.views-row, article.pr-item, li[class*='pr']"):
        a = el.find("a", href=True)
        if not a:
            continue
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.justice.gov" + href
        if "/pr/" not in href and "/opa/pr/" not in href:
            continue

        title = a.get_text(strip=True)
        # Try to find a date within the same element
        date_el = el.find(class_=lambda c: c and "date" in c)
        date_str = date_el.get_text(strip=True) if date_el else ""

        results.append({"url": href, "title": title, "date_str": date_str})

    return results


def _extract_title(raw: bytes) -> str:
    """Extract page title from press release HTML."""
    soup = BeautifulSoup(raw, "lxml")
    for sel in ["h1.page-title", "h1", "title"]:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(strip=True)
            if text and len(text) > 5:
                return text
    return ""


def _extract_body_text(raw: bytes) -> str:
    """Extract main body text from a DOJ press release page."""
    soup = BeautifulSoup(raw, "lxml")
    # Try selectors in preference order
    for sel in [
        ".field-items .field-item",
        ".field-items",
        ".node-body",
        "article .field-item",
        "div[class*='body']",
        "main article",
    ]:
        el = soup.select_one(sel)
        if el:
            return el.get_text(" ", strip=True)
    # Fallback: join all paragraph text
    return " ".join(p.get_text(" ", strip=True) for p in soup.find_all("p"))


def _is_plausible_name(name: str) -> bool:
    """Return True if the extracted string looks like a person's name."""
    if not name:
        return False
    tokens = name.split()
    # Require 2–5 tokens
    if not (2 <= len(tokens) <= 5):
        return False
    # Total length sanity check
    if not (4 <= len(name) <= 60):
        return False
    # First name must not be hyphenated (rules out "Co-defendant", "Ex-officer", etc.)
    if "-" in tokens[0]:
        return False
    # Each substantive token should start with uppercase
    for tok in tokens:
        clean = tok.rstrip(".,;")
        if len(clean) > 2 and clean[0].islower():
            return False
    # No token should be a stopword (case-insensitive); also check hyphen-split parts
    for tok in tokens:
        parts = tok.lower().rstrip(".,").split("-")
        if any(p in _NAME_STOPWORDS for p in parts):
            return False
    # Must pass normalize_name check
    if not normalize_name(name):
        return False
    return True


def _add_individual(
    seen: dict,
    name: str,
    relationship: str,
    confidence: str,
    title: str,
    base_ids: dict,
) -> None:
    """Add or upgrade an individual in the seen dict (prefer higher confidence)."""
    key = normalize_name(name)
    if not key:
        return
    existing = seen.get(key)
    # Upgrade if: not seen before, or upgrading from medium to high
    if existing is None or (confidence == "high" and existing.identifiers.get("confidence") != "high"):
        idents = dict(base_ids)
        idents["confidence"] = confidence
        seen[key] = ParsedIndividual(
            name=name,
            relationship=relationship,
            excerpt=title[:300],
            identifiers=idents,
        )
