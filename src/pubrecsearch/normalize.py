"""Canonical name normalization shared by all scrapers and the query API.

Rules (applied in order):
1. Unicode NFKD normalization, strip combining characters (accents)
2. Lowercase
3. Strip punctuation except hyphens and apostrophes within words
4. Collapse whitespace
"""

import re
import unicodedata


_STRIP_COMBINING = re.compile(r"[\u0300-\u036f]")
_PUNCT = re.compile(r"[^\w\s\-']")
_MULTI_SPACE = re.compile(r"\s+")

# Common organizational suffixes to strip for org name normalization
_ORG_SUFFIXES = re.compile(
    r"\b(llc|l\.l\.c\.|inc|incorporated|corp|corporation|co|ltd|limited|"
    r"lp|llp|pllc|pc|pa|associates|assoc|group|holdings|enterprises)\b\.?$",
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Return a normalized form of a person or organization name.

    This function is the single source of truth for name normalization.
    It MUST be used identically at ingest time and at query time.
    """
    if not name:
        return ""

    # 1. NFKD + strip combining characters (removes accents)
    normalized = unicodedata.normalize("NFKD", name)
    normalized = _STRIP_COMBINING.sub("", normalized)

    # 2. Lowercase
    normalized = normalized.lower()

    # 3. Strip punctuation except hyphens and apostrophes within words
    #    Keep hyphens and apostrophes only when surrounded by word characters
    normalized = _PUNCT.sub(" ", normalized)

    # 4. Collapse whitespace
    normalized = _MULTI_SPACE.sub(" ", normalized).strip()

    return normalized


def normalize_org_name(name: str) -> tuple[str, str | None]:
    """Normalize an organization name, returning (norm, stripped_suffix).

    The suffix (LLC, Inc, etc.) is stripped from the normalized form but
    returned separately so callers can store it if needed.
    """
    norm = normalize_name(name)
    m = _ORG_SUFFIXES.search(norm)
    if m:
        suffix = m.group(0)
        norm = norm[: m.start()].strip()
        return norm, suffix
    return norm, None
