"""Core data transfer objects used across scrapers and the runner."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DownloadTarget:
    """A single file or page to fetch for a given source."""

    url: str
    source_id: str
    period: str        # e.g. "2025-04", "2024-Q1", "2025-04-17"
    doc_type: str      # "csv", "xml", "xlsx", "html", "zip"
    filename: str      # used to build the R2 key
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedIndividual:
    """One named person extracted from a source document."""

    name: str
    relationship: str          # role in the document: "sanctioned_individual", "donor", etc.
    excerpt: str | None = None
    identifiers: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedRecord:
    """One logical record from a source (may name multiple individuals)."""

    individuals: list[ParsedIndividual]
    source_identifier: str | None = None  # native ID from the source, if available
