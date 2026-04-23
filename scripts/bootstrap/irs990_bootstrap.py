#!/usr/bin/env python3
"""IRS Form 990 historical bootstrap — load officer/director names for 2015–present.

Usage:
    python scripts/bootstrap/irs990_bootstrap.py --from-year 2015 --to-year 2023
    python scripts/bootstrap/irs990_bootstrap.py --year 2020 --limit 10000

Downloads the IRS S3 index JSON for each year, then fetches and parses each
individual 990 XML to extract officer/director/trustee names.

Warning: Full historical backfill is a large operation — there are ~150K–200K
990 filings per year, each requiring an individual HTTP request to IRS S3.
Use --limit to process in batches and --resume to continue from where you left off.

State is checkpointed per filing URL so the script can be interrupted and resumed.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.irs_990 import Irs990Scraper, fetch_year_index

log = get_logger("irs990_bootstrap")
SOURCE_ID = "irs_990"


def run_year(year: int, limit: int = 0, force: bool = False) -> None:
    """Download and ingest 990 filings for a given year."""
    log.info("year_start", year=year)

    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    processed_urls = set(meta.get("bootstrap_processed", []))

    entries = fetch_year_index(year)
    log.info("index_loaded", year=year, total=len(entries))

    scraper = Irs990Scraper()
    processed = 0
    new_count = 0

    for entry in entries:
        url = entry.get("URL", "")
        if not url:
            continue
        if url in processed_urls and not force:
            continue

        ein = entry.get("EIN", "")
        company = entry.get("CompanyName", "")
        form_type = entry.get("FormType", "990")
        tax_period = entry.get("TaxPeriod", "")
        period = f"{tax_period[:4]}-{tax_period[4:6]}" if len(tax_period) >= 6 else str(year)

        try:
            with __import__("httpx").Client(timeout=30, headers={"User-Agent": "PubRecSearch/1.0"}) as client:
                resp = client.get(url)
                if resp.status_code == 404:
                    processed_urls.add(url)
                    continue
                resp.raise_for_status()
                raw = resp.content
        except Exception as exc:
            log.warning("fetch_failed", url=url, error=str(exc))
            time.sleep(1)
            continue

        from pubrecsearch.db import sha256, insert_document
        file_hash = sha256(raw)
        filename = url.split("/")[-1]
        key = r2.r2_key(SOURCE_ID, period, filename)

        if not db.document_exists(file_hash):
            try:
                r2.upload(key, raw, "application/xml")
            except Exception as exc:
                log.warning("r2_upload_failed", url=url, error=str(exc))

            doc_id = insert_document(
                r2_key=key,
                source_id=SOURCE_ID,
                source_url=url,
                file_hash=file_hash,
                file_size=len(raw),
                doc_type="xml",
                period=period,
            )

            from pubrecsearch.models import DownloadTarget
            target = DownloadTarget(
                url=url, source_id=SOURCE_ID, period=period,
                doc_type="xml", filename=filename,
                metadata={"ein": ein, "company": company, "form_type": form_type},
            )
            records = scraper.parse(raw, target)

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

        processed_urls.add(url)
        processed += 1

        # Checkpoint every 500 filings
        if processed % 500 == 0:
            meta["bootstrap_processed"] = list(processed_urls)
            db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
            log.info("checkpoint", year=year, processed=processed, new=new_count)
            time.sleep(0.5)  # be polite to IRS S3

        if limit and processed >= limit:
            log.info("limit_reached", year=year, processed=processed)
            break

    meta["bootstrap_processed"] = list(processed_urls)
    db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
    log.info("year_complete", year=year, processed=processed, new_individuals=new_count)


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="IRS Form 990 historical bootstrap")
    parser.add_argument("--year", type=int)
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=2023)
    parser.add_argument("--limit", type=int, default=0, help="Max filings per year (0=all)")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.year:
        years = [args.year]
    else:
        years = list(range(args.from_year, args.to_year + 1))

    for year in years:
        run_year(year, limit=args.limit, force=args.force)


if __name__ == "__main__":
    main()
