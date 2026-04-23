#!/usr/bin/env python3
"""USASpending.gov historical bootstrap — individual award recipients for FY2015–present.

Usage:
    python scripts/bootstrap/usaspending_bootstrap.py --from-year 2015 --to-year 2024
    python scripts/bootstrap/usaspending_bootstrap.py --year 2020

Creates a bulk download job via the USASpending API for each fiscal year,
filtered to individual recipients. Each fiscal year job can take 5–20 minutes.

Fiscal years: FY2015 = Oct 2014 – Sep 2015. Date ranges are adjusted accordingly.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.usaspending import (
    UsaspendingScraper, _extract_csvs, _parse_usaspending_csv,
    _BULK_API, _HEADERS,
)

log = get_logger("usaspending_bootstrap")
SOURCE_ID = "usaspending"


def _fy_dates(fy: int) -> tuple[str, str]:
    """Return (start_date, end_date) for a US federal fiscal year."""
    return f"{fy - 1}-10-01", f"{fy}-09-30"


def run_fiscal_year(fy: int, force: bool = False) -> None:
    """Download and ingest USASpending individual awards for one fiscal year."""
    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    done_years = meta.get("bootstrap_years", [])
    if str(fy) in done_years and not force:
        log.info("year_already_processed", fy=fy)
        return

    start_date, end_date = _fy_dates(fy)
    period = f"FY{fy}"
    log.info("year_start", fy=fy, start=start_date, end=end_date)
    job_id = db.start_job(SOURCE_ID)

    scraper = UsaspendingScraper()

    import shutil, tempfile
    from pubrecsearch.models import DownloadTarget
    target = DownloadTarget(
        url=_BULK_API,
        source_id=SOURCE_ID,
        period=period,
        doc_type="zip",
        filename=f"usaspending_{period}.zip",
        metadata={"start_date": start_date, "end_date": end_date, "cursor_after": period},
    )

    try:
        raw = scraper.fetch(target)
    except Exception as exc:
        log.error("fetch_failed", fy=fy, error=str(exc))
        db.finish_job(job_id, "failed", notes=str(exc))
        return

    from pubrecsearch.db import sha256, insert_document
    file_hash = sha256(raw)
    key = r2.r2_key(SOURCE_ID, period, f"usaspending_{period}.zip")

    try:
        r2.upload(key, raw, "application/zip")
    except Exception as exc:
        log.warning("r2_upload_failed", fy=fy, error=str(exc))

    doc_id = insert_document(
        r2_key=key, source_id=SOURCE_ID, source_url=_BULK_API,
        file_hash=file_hash, file_size=len(raw), doc_type="zip", period=period,
    )

    tmp_dir = tempfile.mkdtemp()
    try:
        csv_paths = _extract_csvs(raw, tmp_dir)
        records = []
        for path in csv_paths:
            records.extend(_parse_usaspending_csv(path, target))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    log.info("parsed", fy=fy, records=len(records))
    new_count = 0
    for record in records:
        for person in record.individuals:
            name_norm = normalize_name(person.name)
            ind_id = db.insert_individual(person.name, name_norm)
            db.insert_individual_document(
                individual_id=ind_id, document_id=doc_id,
                relationship=person.relationship, excerpt=person.excerpt,
                identifiers=person.identifiers or None,
            )
            new_count += 1

    db.finish_job(job_id, "success", records_processed=len(records), records_new=new_count)
    meta["bootstrap_years"] = done_years + [str(fy)]
    db.set_state(SOURCE_ID, cursor=period, metadata=meta)
    log.info("year_complete", fy=fy, new_individuals=new_count)


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="USASpending historical bootstrap")
    parser.add_argument("--year", type=int, help="Single fiscal year")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=2024)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    years = [args.year] if args.year else list(range(args.from_year, args.to_year + 1))
    for fy in years:
        run_fiscal_year(fy, force=args.force)


if __name__ == "__main__":
    main()
