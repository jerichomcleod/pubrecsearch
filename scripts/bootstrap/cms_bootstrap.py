#!/usr/bin/env python3
"""CMS Open Payments historical bootstrap — load physician payments for 2013–present.

Usage:
    python scripts/bootstrap/cms_bootstrap.py --from-year 2015 --to-year 2023
    python scripts/bootstrap/cms_bootstrap.py --year 2020

CMS publishes annual "General Payment" files (~2-4 GB compressed per year).
Each file is a ZIP containing a large CSV of physician payment records.

Direct download URLs follow the pattern:
    https://download.cms.gov/openpayments/PGYR{YY}_P{date}.ZIP

The exact date suffix changes each year when the file is refreshed. This script
uses a known URL map for historical files and falls back to the data portal API.

Run this ONCE for historical backfill; CmsOpenPaymentsScraper handles annual updates.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.cms_open_payments import CmsOpenPaymentsScraper, _extract_csv, _parse_cms_csv

log = get_logger("cms_bootstrap")
SOURCE_ID = "cms_open_payments"

# Known stable URLs for CMS historical program year files
# These are updated annually; verify URLs at https://openpaymentsdata.cms.gov/
_YEAR_URLS: dict[int, str] = {
    2013: "https://download.cms.gov/openpayments/PGYR13_P01302024.ZIP",
    2014: "https://download.cms.gov/openpayments/PGYR14_P01302024.ZIP",
    2015: "https://download.cms.gov/openpayments/PGYR15_P01302024.ZIP",
    2016: "https://download.cms.gov/openpayments/PGYR16_P01302024.ZIP",
    2017: "https://download.cms.gov/openpayments/PGYR17_P01302024.ZIP",
    2018: "https://download.cms.gov/openpayments/PGYR18_P01302024.ZIP",
    2019: "https://download.cms.gov/openpayments/PGYR19_P01302024.ZIP",
    2020: "https://download.cms.gov/openpayments/PGYR20_P01302024.ZIP",
    2021: "https://download.cms.gov/openpayments/PGYR21_P01302024.ZIP",
    2022: "https://download.cms.gov/openpayments/PGYR22_P01302024.ZIP",
    2023: "https://download.cms.gov/openpayments/PGYR23_P06282024.ZIP",
}


def run_year(year: int, force: bool = False) -> None:
    """Download and ingest CMS Open Payments for one program year."""
    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    done_years = meta.get("bootstrap_years", [])
    if str(year) in done_years and not force:
        log.info("year_already_processed", year=year)
        return

    url = _YEAR_URLS.get(year)
    if not url:
        log.error("no_url_for_year", year=year)
        return

    log.info("year_start", year=year, url=url)
    period = str(year)
    job_id = db.start_job(SOURCE_ID)

    import httpx, shutil, tempfile, os
    try:
        with httpx.Client(timeout=600, follow_redirects=True,
                          headers={"User-Agent": "PubRecSearch/1.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except Exception as exc:
        log.error("fetch_failed", year=year, error=str(exc))
        db.finish_job(job_id, "failed", notes=str(exc))
        return

    from pubrecsearch.db import sha256, insert_document
    file_hash = sha256(raw)
    key = r2.r2_key(SOURCE_ID, period, f"cms_open_payments_{period}.zip")

    if not db.document_exists(file_hash):
        try:
            r2.upload(key, raw, "application/zip")
        except Exception as exc:
            log.warning("r2_upload_failed", year=year, error=str(exc))

        doc_id = insert_document(
            r2_key=key, source_id=SOURCE_ID, source_url=url,
            file_hash=file_hash, file_size=len(raw), doc_type="zip", period=period,
        )

        tmp_dir = tempfile.mkdtemp()
        try:
            from pubrecsearch.models import DownloadTarget
            target = DownloadTarget(url=url, source_id=SOURCE_ID, period=period,
                                    doc_type="zip", filename=f"cms_open_payments_{period}.zip",
                                    metadata={"program_year": period})
            csv_path = _extract_csv(raw, tmp_dir)
            records = _parse_cms_csv(csv_path, target)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        log.info("parsed", year=year, records=len(records))
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
        log.info("year_complete", year=year, new_individuals=new_count)
    else:
        log.info("document_unchanged", year=year)
        db.finish_job(job_id, "success")

    meta["bootstrap_years"] = done_years + [str(year)]
    db.set_state(SOURCE_ID, cursor=period, metadata=meta)


def main():
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="CMS Open Payments historical bootstrap")
    parser.add_argument("--year", type=int)
    parser.add_argument("--from-year", type=int, default=2015)
    parser.add_argument("--to-year", type=int, default=2023)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    years = [args.year] if args.year else list(range(args.from_year, args.to_year + 1))
    for year in years:
        run_year(year, force=args.force)


if __name__ == "__main__":
    main()
