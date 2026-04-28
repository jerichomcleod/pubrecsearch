#!/usr/bin/env python3
"""EPA ECHO individual enforcement defendant bootstrap — load cases for 2015–present.

Fetches all EPA enforcement cases with individual defendants year by year,
stores the combined JSON in R2, and bulk-inserts defendants into PostgreSQL.

Usage:
    python scripts/bootstrap/epa_bootstrap.py
    python scripts/bootstrap/epa_bootstrap.py --from-year 2015 --to-year 2020
    python scripts/bootstrap/epa_bootstrap.py --year 2022
    python scripts/bootstrap/epa_bootstrap.py --year 2023 --force

Rate limit: 2 req/sec enforced. Full historical run may take several hours
depending on case count (estimated 5K–15K individual cases since 2015).

Progress is checkpointed by year in scrape_state.metadata.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.db import sha256, insert_document, bulk_insert_individuals, get_conn
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.scrapers.epa_echo import (
    fetch_case_ids_for_year,
    parse_cases,
    _fetch_case_detail,
    _REQ_DELAY,
)

log = get_logger("epa_bootstrap")
SOURCE_ID = "epa_echo"


def run_year(year: int, force: bool = False) -> None:
    """Download and ingest all individual-defendant EPA cases active in a given year."""
    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    completed_years: list[int] = meta.get("bootstrap_completed_years", [])

    if year in completed_years and not force:
        log.info("year_already_done", year=year)
        return

    log.info("year_start", year=year)

    case_ids = fetch_case_ids_for_year(year)
    log.info("case_ids_found", year=year, count=len(case_ids))

    if not case_ids:
        completed_years.append(year)
        meta["bootstrap_completed_years"] = sorted(set(completed_years))
        db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
        return

    # Fetch case details
    cases: list[dict] = []
    failed = 0
    for i, case_id in enumerate(case_ids):
        try:
            detail = _fetch_case_detail(case_id)
            if detail:
                cases.append(detail)
        except Exception as exc:
            log.warning("case_detail_failed", case_id=case_id, error=str(exc))
            failed += 1

        if (i + 1) % 100 == 0:
            log.info("progress", year=year, fetched=i + 1, total=len(case_ids))

        time.sleep(_REQ_DELAY)

    log.info(
        "cases_fetched", year=year,
        fetched=len(cases), failed=failed, total=len(case_ids),
    )

    if not cases:
        completed_years.append(year)
        meta["bootstrap_completed_years"] = sorted(set(completed_years))
        db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
        return

    # Upload combined JSON to R2
    raw_json = json.dumps(cases).encode()
    file_hash = sha256(raw_json)
    filename = f"epa_echo_individual_{year}.json"
    key = r2.r2_key(SOURCE_ID, str(year), filename)

    if db.document_exists(file_hash):
        log.info("already_in_r2", year=year, hash=file_hash[:12])
    else:
        r2.upload(key, raw_json, "application/json")
        log.info("r2_uploaded", year=year, key=key, size_mb=round(len(raw_json) / 1e6, 2))

    doc_id = insert_document(
        r2_key=key,
        source_id=SOURCE_ID,
        source_url=f"https://echodata.epa.gov/echo/case_rest_services.get_cases?p_defendant_type=I&year={year}",
        file_hash=file_hash,
        file_size=len(raw_json),
        doc_type="json",
        period=str(year),
    )

    # Parse and bulk-insert defendants
    records = parse_cases(cases)
    rows: list[dict] = []
    from pubrecsearch.normalize import normalize_name
    for rec in records:
        for ind in rec.individuals:
            nrm = normalize_name(ind.name)
            if nrm:
                rows.append({
                    "name": ind.name,
                    "name_norm": nrm,
                    "excerpt": ind.excerpt or "",
                    "identifiers": ind.identifiers or {},
                })

    total_new = 0
    total_rows = 0
    batch_size = 5_000
    with get_conn() as conn:
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            ins, tot = bulk_insert_individuals(
                conn, doc_id, "epa_enforcement_defendant", batch
            )
            total_new += ins
            total_rows += tot

    log.info(
        "year_complete",
        year=year,
        cases=len(cases),
        defendants=total_rows,
        individuals_new=total_new,
    )

    # Checkpoint
    completed_years.append(year)
    meta["bootstrap_completed_years"] = sorted(set(completed_years))
    db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)


def main() -> None:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="EPA ECHO individual enforcement defendant bootstrap")
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
            time.sleep(10)

    log.info("bootstrap_complete")


if __name__ == "__main__":
    main()
