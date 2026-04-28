#!/usr/bin/env python3
"""LDA Lobbying Disclosure Act historical bootstrap — load filings for 2015–present.

Fetches all LD-2 filings year by year from the LDA REST API, bulk-inserts
named lobbyists into PostgreSQL, and uploads the raw JSON per year to R2.

Requires LDA_API_KEY in .env. Rate limit: 120 req/min (0.5 s delay enforced).

Usage:
    python scripts/bootstrap/lda_bootstrap.py
    python scripts/bootstrap/lda_bootstrap.py --from-year 2015 --to-year 2020
    python scripts/bootstrap/lda_bootstrap.py --year 2022
    python scripts/bootstrap/lda_bootstrap.py --year 2023 --force

Progress is checkpointed in scrape_state.metadata so interrupted runs resume
from the last completed year without re-fetching.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.db import sha256
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.scrapers.lda import fetch_year_filings, filings_to_rows
from pubrecsearch.db import bulk_insert_individuals, get_conn

log = get_logger("lda_bootstrap")
SOURCE_ID = "lda"


def run_year(year: int, force: bool = False) -> None:
    """Download, store, and ingest all LDA filings for a single year."""
    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    completed_years: list[int] = meta.get("bootstrap_completed_years", [])

    if year in completed_years and not force:
        log.info("year_already_done", year=year)
        return

    log.info("year_start", year=year)

    # Fetch all filings for this year (paginated API calls)
    filings = fetch_year_filings(year)
    log.info("filings_fetched", year=year, count=len(filings))

    if not filings:
        log.warning("no_filings", year=year)
        completed_years.append(year)
        meta["bootstrap_completed_years"] = sorted(set(completed_years))
        db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
        return

    # Upload raw JSON to R2
    raw_json = json.dumps(filings).encode()
    file_hash = sha256(raw_json)
    filename = f"lda_filings_{year}.json"
    key = r2.r2_key(SOURCE_ID, str(year), filename)

    if db.document_exists(file_hash):
        log.info("already_in_r2", year=year, hash=file_hash[:12])
    else:
        r2.upload(key, raw_json, "application/json")
        log.info("r2_uploaded", year=year, key=key, size_mb=round(len(raw_json) / 1e6, 2))

    doc_id = db.insert_document(
        r2_key=key,
        source_id=SOURCE_ID,
        source_url=f"https://lda.senate.gov/api/v1/filings/?filing_year={year}",
        file_hash=file_hash,
        file_size=len(raw_json),
        doc_type="json",
        period=str(year),
    )

    # Bulk-insert lobbyists in batches of 10,000 rows
    rows = filings_to_rows(filings)
    log.info("rows_prepared", year=year, rows=len(rows))

    total_inserted = 0
    total_rows = 0
    batch_size = 10_000

    with get_conn() as conn:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            inserted, total = bulk_insert_individuals(conn, doc_id, "lda_lobbyist", batch)
            total_inserted += inserted
            total_rows += total

    log.info(
        "year_complete",
        year=year,
        filings=len(filings),
        rows_processed=total_rows,
        individuals_new=total_inserted,
    )

    # Checkpoint
    completed_years.append(year)
    meta["bootstrap_completed_years"] = sorted(set(completed_years))
    db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)


def main() -> None:
    configure_logging("INFO")

    parser = argparse.ArgumentParser(
        description="LDA historical bootstrap (2015–present)"
    )
    parser.add_argument("--year", type=int, help="Process a single year")
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument(
        "--to-year", type=int, default=date.today().year,
        help="Last year to process (default: current year)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-process years already marked complete",
    )
    args = parser.parse_args()

    years = [args.year] if args.year else list(range(args.from_year, args.to_year + 1))
    log.info("bootstrap_start", years=years)

    for year in years:
        try:
            run_year(year, force=args.force)
        except Exception as exc:
            log.error("year_failed", year=year, error=str(exc))
            time.sleep(5)

    log.info("bootstrap_complete")


if __name__ == "__main__":
    main()
