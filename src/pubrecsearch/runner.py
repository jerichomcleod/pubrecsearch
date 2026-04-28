"""Scrape job runner — orchestrates discover → fetch → parse → store for a single source.

The ScraperRunner handles all cross-cutting concerns:
  - scrape_jobs tracking (start / finish)
  - R2 upload (must succeed before PostgreSQL write)
  - individual + individual_documents insertion
  - per-target error handling per the policy in implementation_plan.md §5

Large-file streaming pattern (scrapers > ~1 GB):
  If a scraper defines `fetch_to_file(target) -> Path`, the runner uses a
  streaming path that avoids loading the full file into memory:
    1. Scraper streams download to a temp file
    2. Runner hashes the file incrementally (no full-read into RAM)
    3. Runner uploads via r2.upload_fileobj (streaming multipart to R2)
    4. Runner calls parse_to_db(raw=None, ...) — scraper reads from the
       temp path stored in target.metadata["_local_path"]

Usage:
  runner = ScraperRunner(OfacSdnScraper())
  runner.run()
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import tenacity
from tenacity import retry, stop_after_attempt, wait_exponential

from . import db, r2
from .base_scraper import BaseScraper
from .models import DownloadTarget, ParsedRecord
from .monitoring import get_logger, send_job_failure_alert
from .normalize import normalize_name


class ScraperRunner:
    def __init__(self, scraper: BaseScraper) -> None:
        self.scraper = scraper
        self.log = get_logger(scraper.source_id)

    def run(self) -> dict[str, Any]:
        """Execute one full scrape cycle. Returns a summary dict."""
        source_id = self.scraper.source_id
        self.log.info("job_start")

        state = db.get_state(source_id)
        job_id = db.start_job(source_id)

        records_processed = 0
        records_new = 0
        errors_count = 0
        last_good_cursor: str | None = state.get("cursor")

        try:
            targets = self.scraper.discover(state)
            self.log.info("targets_discovered", count=len(targets))
        except Exception as exc:
            self.log.error("discover_failed", error=str(exc), exc_info=True)
            db.finish_job(job_id, "failed", notes=f"discover failed: {exc}")
            send_job_failure_alert(source_id, job_id, f"discover failed: {exc}")
            return {"status": "failed", "job_id": job_id}

        for target in targets:
            try:
                raw, file_hash, doc_id, is_new = self._fetch_and_store_document(target, job_id)
            except _SkipTarget:
                self.log.info("target_skipped_unchanged", url=target.url)
                continue
            except Exception as exc:
                errors_count += 1
                self.log.error("target_failed", url=target.url, error=str(exc), exc_info=True)
                db.log_error(job_id, "error", f"target failed: {target.url}", {"error": str(exc)})
                continue

            if not is_new:
                self.log.info("document_unchanged", url=target.url)
                continue

            # Large sources (FEC, CMS, etc.) implement parse_to_db() for
            # bulk COPY performance — call it instead of per-row parse().
            # When using the streaming fetch_to_file() path, raw is None and
            # the scraper reads from target.metadata["_local_path"] instead.
            if hasattr(self.scraper, "parse_to_db"):
                local_path_str = target.metadata.get("_local_path")
                try:
                    processed, new = self.scraper.parse_to_db(raw, target, doc_id)
                    records_processed += processed
                    records_new += new
                    self.log.info(
                        "bulk_insert_complete",
                        url=target.url,
                        processed=processed,
                        new=new,
                    )
                except Exception as exc:
                    errors_count += 1
                    self.log.error(
                        "bulk_parse_failed", url=target.url, error=str(exc), exc_info=True
                    )
                    db.log_error(
                        job_id, "critical", f"bulk parse failed: {target.url}", {"error": str(exc)}
                    )
                finally:
                    # Clean up temp file from streaming fetch_to_file() path
                    if local_path_str:
                        try:
                            os.unlink(local_path_str)
                        except Exception:
                            pass
                continue

            try:
                records = self.scraper.parse(raw, target)
            except Exception as exc:
                errors_count += 1
                self.log.error(
                    "parse_failed_entire_file", url=target.url, error=str(exc), exc_info=True
                )
                db.log_error(job_id, "critical", f"parse failed: {target.url}", {"error": str(exc)})
                # Do not advance cursor — will retry next run
                continue

            for record in records:
                records_processed += 1
                try:
                    newly_inserted = self._write_record(record, doc_id)
                    if newly_inserted:
                        records_new += newly_inserted
                except Exception as exc:
                    errors_count += 1
                    self.log.warning(
                        "record_write_failed",
                        source_identifier=record.source_identifier,
                        error=str(exc),
                    )
                    db.log_error(
                        job_id,
                        "warning",
                        "record write failed",
                        {"source_identifier": record.source_identifier, "error": str(exc)},
                    )

            last_good_cursor = target.metadata.get("cursor_after") or last_good_cursor

        final_status = "success" if errors_count == 0 else ("partial" if records_processed > 0 else "failed")
        db.finish_job(job_id, final_status, records_processed, records_new, errors_count)

        if final_status == "success":
            db.set_state(
                source_id,
                cursor=last_good_cursor,
                metadata=state.get("metadata"),
            )

        self.log.info(
            "job_finish",
            status=final_status,
            records_processed=records_processed,
            records_new=records_new,
            errors=errors_count,
        )

        if final_status == "failed":
            send_job_failure_alert(source_id, job_id, f"0 records processed, {errors_count} errors")

        return {
            "status": final_status,
            "job_id": job_id,
            "records_processed": records_processed,
            "records_new": records_new,
            "errors_count": errors_count,
        }

    # ------------------------------------------------------------------

    def _fetch_and_store_document(
        self, target: DownloadTarget, job_id: int
    ) -> tuple[bytes | None, str, int, bool]:
        """Fetch raw bytes (or stream to file), upload to R2, insert document row.

        Returns (raw_or_None, file_hash, document_id, is_new).
        When the scraper implements fetch_to_file(), raw is None and
        target.metadata["_local_path"] holds the temp file path.
        Raises _SkipTarget if the file is unchanged (ETag / hash match).
        Raises on R2 or DB failure.
        """
        key = r2.r2_key(target.source_id, target.period, target.filename)
        content_type = r2.CONTENT_TYPES.get(target.doc_type, "application/octet-stream")

        if hasattr(self.scraper, "fetch_to_file"):
            # Streaming path: scraper writes to temp file, we hash+upload without
            # loading the full file into RAM.
            local_path: Path = self._fetch_to_file_with_retry(target)
            target.metadata["_local_path"] = str(local_path)

            file_hash = _hash_file(local_path)
            file_size = local_path.stat().st_size

            if db.document_exists(file_hash):
                try:
                    local_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise _SkipTarget()

            with open(local_path, "rb") as fobj:
                self._upload_fileobj_with_retry(key, fobj, content_type)

            doc_id = db.insert_document(
                r2_key=key,
                source_id=target.source_id,
                source_url=target.url,
                file_hash=file_hash,
                file_size=file_size,
                doc_type=target.doc_type,
                period=target.period,
            )
            return None, file_hash, doc_id, True

        # Standard in-memory path
        raw = self._fetch_with_retry(target)
        file_hash = db.sha256(raw)

        if db.document_exists(file_hash):
            raise _SkipTarget()

        self._upload_with_retry(key, raw, content_type)

        doc_id = db.insert_document(
            r2_key=key,
            source_id=target.source_id,
            source_url=target.url,
            file_hash=file_hash,
            file_size=len(raw),
            doc_type=target.doc_type,
            period=target.period,
        )
        return raw, file_hash, doc_id, True

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=tenacity.retry_if_exception_type(Exception),
        reraise=True,
    )
    def _fetch_with_retry(self, target: DownloadTarget) -> bytes:
        return self.scraper.fetch(target)

    @retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(multiplier=1, min=5, max=60),
        retry=tenacity.retry_if_exception_type(Exception),
        reraise=True,
    )
    def _fetch_to_file_with_retry(self, target: DownloadTarget) -> Path:
        return self.scraper.fetch_to_file(target)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    def _upload_with_retry(self, key: str, data: bytes, content_type: str) -> None:
        r2.upload(key, data, content_type)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=5, max=120),
        reraise=True,
    )
    def _upload_fileobj_with_retry(self, key: str, fileobj, content_type: str) -> None:
        r2.upload_fileobj(key, fileobj, content_type)

    def _write_record(self, record: ParsedRecord, doc_id: int) -> int:
        """Insert individual rows and linkages. Returns count of new individuals."""
        count = 0
        for person in record.individuals:
            name_norm = normalize_name(person.name)
            ind_id = db.insert_individual(person.name, name_norm)
            db.insert_individual_document(
                individual_id=ind_id,
                document_id=doc_id,
                relationship=person.relationship,
                excerpt=person.excerpt,
                identifiers=person.identifiers or None,
            )
            count += 1
        return count


def _hash_file(path: Path, chunk: int = 8 * 1024 * 1024) -> str:
    """Compute SHA-256 of a file without loading it fully into memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


class _SkipTarget(Exception):
    """Internal signal: target is unchanged, skip without error."""


# ---------------------------------------------------------------------------
# APScheduler integration
# ---------------------------------------------------------------------------


def build_scheduler(scrapers: list[BaseScraper]):
    """Return a configured APScheduler BlockingScheduler with all scrapers registered."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BlockingScheduler()
    for scraper in scrapers:
        runner = ScraperRunner(scraper)
        parts = scraper.schedule.split()
        trigger = CronTrigger(
            minute=parts[0],
            hour=parts[1],
            day=parts[2],
            month=parts[3],
            day_of_week=parts[4],
        )
        scheduler.add_job(
            runner.run,
            trigger=trigger,
            id=scraper.source_id,
            name=scraper.source_id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        get_logger().info("scraper_registered", source_id=scraper.source_id, schedule=scraper.schedule)

    return scheduler
