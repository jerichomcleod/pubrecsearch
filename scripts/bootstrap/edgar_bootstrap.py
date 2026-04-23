#!/usr/bin/env python3
"""SEC EDGAR historical bootstrap — load Form 4/3/SC 13G filings for 2015–present.

Usage:
    python scripts/bootstrap/edgar_bootstrap.py --from-year 2015 --to-year 2024
    python scripts/bootstrap/edgar_bootstrap.py --year 2020

Processes quarterly form.gz index files. Each file is ~5-15 MB compressed.
Extracts Form 4, 3, SC 13G/A filer names (insider reporters, beneficial owners).

State is checkpointed per quarter so the script can be interrupted and resumed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.sec_edgar import fetch_quarterly_index, parse_quarterly_index

log = get_logger("edgar_bootstrap")
SOURCE_ID = "sec_edgar"


def run_quarter(year: int, qtr: int, force: bool = False) -> None:
    """Download and ingest one EDGAR quarterly index."""
    period = f"{year}-Q{qtr}"

    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    done = meta.get("bootstrap_quarters", [])
    if period in done and not force:
        log.info("quarter_already_processed", period=period)
        return

    log.info("quarter_start", period=period)
    job_id = db.start_job(SOURCE_ID)

    try:
        raw = fetch_quarterly_index(year, qtr)
    except Exception as exc:
        log.error("fetch_failed", period=period, error=str(exc))
        db.finish_job(job_id, "failed", notes=str(exc))
        return

    # Upload to R2
    key = r2.r2_key(SOURCE_ID, period, f"edgar_{year}_Q{qtr}.gz")
    try:
        r2.upload(key, raw, "application/gzip")
    except Exception as exc:
        log.warning("r2_upload_failed", period=period, error=str(exc))

    from pubrecsearch.db import sha256, insert_document
    file_hash = sha256(raw)
    doc_id = insert_document(
        r2_key=key,
        source_id=SOURCE_ID,
        source_url=f"https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{qtr}/form.gz",
        file_hash=file_hash,
        file_size=len(raw),
        doc_type="gz",
        period=period,
    )

    records = parse_quarterly_index(raw, year, qtr)
    log.info("parsed", period=period, records=len(records))

    new_count = 0
    for record in records:
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
            new_count += 1

    db.finish_job(job_id, "success", records_processed=len(records), records_new=new_count)

    # Checkpoint
    state = db.get_state(SOURCE_ID)
    meta = (state.get("metadata") or {})
    done = meta.get("bootstrap_quarters", [])
    if period not in done:
        done.append(period)
    meta["bootstrap_quarters"] = done
    db.set_state(SOURCE_ID, cursor=period, metadata=meta)
    log.info("quarter_complete", period=period, new_individuals=new_count)


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="SEC EDGAR historical bootstrap")
    parser.add_argument("--year", type=int, help="Single year")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=2024)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.year:
        years = [args.year]
    else:
        years = list(range(args.from_year, args.to_year + 1))

    for year in years:
        for qtr in range(1, 5):
            run_quarter(year, qtr, force=args.force)


if __name__ == "__main__":
    main()
