#!/usr/bin/env python3
"""FEC historical bootstrap — load individual contributions for 2015–present.

Usage:
    python scripts/bootstrap/fec_bootstrap.py --from-cycle 2016 --to-cycle 2026
    python scripts/bootstrap/fec_bootstrap.py --cycle 2018

FEC uses 2-year election cycles; files are published per cycle-end year (even numbers).
Cycles in scope: 2016, 2018, 2020, 2022, 2024, 2026.

Each cycle file is ~300-700 MB compressed, 2-5 GB uncompressed.
The script downloads, stores in R2, and inserts all individual contributors.
State is checkpointed so the script can be interrupted and resumed.

Run this ONCE to backfill; the ongoing FecContributionsScraper handles current cycle.
"""

import argparse
import sys
from pathlib import Path

# Ensure package is importable when run from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.runner import ScraperRunner
from pubrecsearch.scrapers.fec_contributions import FecContributionsScraper, _cycle_url

log = get_logger("fec_bootstrap")


def run_cycle(year: int, force: bool = False) -> None:
    """Download and ingest one FEC election cycle."""
    source_id = "fec_contributions"
    period = str(year)

    # Check if already processed
    state = db.get_state(source_id)
    metadata = state.get("metadata") or {}
    processed_cycles = metadata.get("bootstrap_cycles", [])
    if str(year) in processed_cycles and not force:
        log.info("cycle_already_processed", year=year)
        return

    log.info("cycle_start", year=year)
    scraper = FecContributionsScraper()
    runner = ScraperRunner(scraper)

    from pubrecsearch.models import DownloadTarget
    target = DownloadTarget(
        url=_cycle_url(year),
        source_id=source_id,
        period=period,
        doc_type="zip",
        filename=f"fec_indiv_{period}.zip",
        metadata={"cursor_after": period},
    )

    # Use runner internals: fetch, store, parse, write
    try:
        raw, file_hash, doc_id, is_new = runner._fetch_and_store_document(target, job_id=0)
    except Exception as exc:
        log.error("fetch_failed", year=year, error=str(exc))
        return

    if is_new:
        records = scraper.parse(raw, target)
        log.info("parsing_complete", year=year, records=len(records))

        for record in records:
            try:
                runner._write_record(record, doc_id)
            except Exception as exc:
                log.warning("record_write_failed", error=str(exc))

    # Mark cycle done in metadata
    state = db.get_state(source_id)
    meta = (state.get("metadata") or {})
    cycles = meta.get("bootstrap_cycles", [])
    if str(year) not in cycles:
        cycles.append(str(year))
    meta["bootstrap_cycles"] = cycles
    db.set_state(source_id, cursor=state.get("cursor"), metadata=meta)
    log.info("cycle_complete", year=year)


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="FEC historical bootstrap")
    parser.add_argument("--cycle", type=int, help="Single cycle year (even number)")
    parser.add_argument("--from-cycle", type=int, default=2016)
    parser.add_argument("--to-cycle", type=int, default=2024)
    parser.add_argument("--force", action="store_true", help="Re-process even if already done")
    args = parser.parse_args()

    if args.cycle:
        cycles = [args.cycle]
    else:
        cycles = list(range(args.from_cycle, args.to_cycle + 1, 2))

    for year in cycles:
        if year % 2 != 0:
            print(f"Skipping {year} — FEC cycles end in even years only")
            continue
        run_cycle(year, force=args.force)


if __name__ == "__main__":
    main()
