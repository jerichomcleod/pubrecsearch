"""Abstract base class that every data-source scraper must implement."""

from abc import ABC, abstractmethod

from .models import DownloadTarget, ParsedRecord


class BaseScraper(ABC):
    """Each data source is a subclass with source_id, schedule, and doc_type set."""

    source_id: str   # snake_case; matches scrape_state.source_id in PostgreSQL
    schedule: str    # cron expression: "0 2 * * 1" = Monday 2 AM
    doc_type: str = "unknown"

    @abstractmethod
    def discover(self, state: dict) -> list[DownloadTarget]:
        """Return the list of files/pages to fetch.

        Use `state` (the scrape_state row for this source) to skip targets
        that have already been successfully processed.
        """

    @abstractmethod
    def fetch(self, target: DownloadTarget) -> bytes:
        """Download raw content for a target. Raise on HTTP error.

        Retry logic (tenacity) belongs in this method, not the runner.
        """

    @abstractmethod
    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        """Extract structured records from raw bytes.

        Each ParsedRecord contains one or more ParsedIndividuals.
        Raise on unrecoverable parse errors; return partial results on
        per-record errors (log them yourself and continue).
        """
