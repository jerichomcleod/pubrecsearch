#!/usr/bin/env python3
"""One-shot SAM.gov exclusions import from a locally downloaded CSV extract.

Usage:
    python scripts/bootstrap/sam_exclusions_import.py --file tmp/SAM_Exclusions_Public_Extract_V2_26113.CSV

This is a one-off for when the CSV is already on disk (e.g. manually downloaded
to work around the 10 req/day API rate limit). The automated daily scraper
(SamExclusionsScraper) handles subsequent runs via the async extract API.

The script:
  1. Reads the CSV from disk
  2. Uploads to R2 for archival
  3. Parses with SamExclusionsScraper.parse() — filters to individuals
  4. Bulk-inserts via db.bulk_insert_individuals() (COPY path)
  5. Advances the scrape_state cursor to today so the daily scraper picks up from here
"""

import argparse
import io
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.db import bulk_insert_individuals, get_conn
from pubrecsearch.models import DownloadTarget
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.sam_exclusions import SamExclusionsScraper

log = get_logger("sam_exclusions_import")

_BATCH_SIZE = 10_000


def run(csv_path: Path, dry_run: bool = False) -> None:
    today = date.today().isoformat()
    filename = csv_path.name
    source_id = "sam_exclusions"

    log.info("reading_file", path=str(csv_path), size_mb=round(csv_path.stat().st_size / 1e6, 1))
    raw = csv_path.read_bytes()

    file_hash = db.sha256(raw)

    if db.document_exists(file_hash):
        log.info("already_imported", file_hash=file_hash)
        print("File already imported (hash match). Use --force to re-import.")
        return

    if not dry_run:
        key = r2.r2_key(source_id, today, filename)
        log.info("uploading_to_r2", key=key)
        r2.upload(key, raw, "text/csv")

        doc_id = db.insert_document(
            r2_key=key,
            source_id=source_id,
            source_url="file://" + str(csv_path.resolve()),
            file_hash=file_hash,
            file_size=len(raw),
            doc_type="csv",
            period=today,
        )
        log.info("document_inserted", doc_id=doc_id)
    else:
        doc_id = -1
        log.info("dry_run_skip_r2_and_db")

    target = DownloadTarget(
        url="file://" + str(csv_path.resolve()),
        source_id=source_id,
        period=today,
        doc_type="csv",
        filename=filename,
        metadata={"cursor_after": today},
    )

    scraper = SamExclusionsScraper()
    log.info("parsing_csv")
    records = scraper.parse(raw, target)
    log.info("parse_complete", records=len(records))

    if dry_run:
        print(f"Dry run: would insert {len(records)} records")
        if records:
            sample = records[:3]
            for r in sample:
                print(" ", r.individuals[0].name, "|", r.individuals[0].excerpt)
        return

    # Bulk insert in batches
    total_processed = 0
    total_new = 0

    for batch_start in range(0, len(records), _BATCH_SIZE):
        batch = records[batch_start : batch_start + _BATCH_SIZE]
        rows = []
        for rec in batch:
            for person in rec.individuals:
                rows.append(
                    {
                        "name": person.name,
                        "name_norm": normalize_name(person.name),
                        "excerpt": person.excerpt or "",
                        "identifiers": person.identifiers or {},
                    }
                )

        with get_conn() as conn:
            inserted, attempted = bulk_insert_individuals(conn, doc_id, "sam_excluded_individual", rows)
            total_processed += attempted
            total_new += inserted

        pct = round((batch_start + len(batch)) / len(records) * 100)
        log.info("batch_complete", progress=f"{pct}%", processed=total_processed, new=total_new)

    log.info("import_complete", total_processed=total_processed, total_new=total_new)

    # Advance cursor so daily scraper knows we're current
    db.set_state(source_id, cursor=today, metadata={"bootstrap_date": today})
    log.info("cursor_advanced", cursor=today)
    print(f"Done. Processed {total_processed} individuals, {total_new} new. Cursor set to {today}.")


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="Import a local SAM.gov exclusions CSV")
    parser.add_argument(
        "--file",
        type=Path,
        default=Path("tmp/SAM_Exclusions_Public_Extract_V2_26113.CSV"),
        help="Path to the CSV extract file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and count records without writing to DB or R2",
    )
    args = parser.parse_args()

    if not args.file.exists():
        print(f"File not found: {args.file}")
        sys.exit(1)

    run(args.file, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
