#!/usr/bin/env python3
"""DOJ Press Releases historical bootstrap — load individual defendant records for 2015–present.

Paginates the DOJ press release listing HTML, extracts defendant names via regex,
stores raw HTML in R2, and bulk-inserts individuals into PostgreSQL.

Usage:
    python scripts/bootstrap/doj_bootstrap.py
    python scripts/bootstrap/doj_bootstrap.py --from-date 2020-01-01
    python scripts/bootstrap/doj_bootstrap.py --max-pages 500 --titles-only

Options:
    --from-date DATE   Earliest publication date to process (default: 2015-01-01)
    --to-date DATE     Latest publication date to process (default: today)
    --max-pages N      Max listing pages to walk (each page has ~20 releases)
    --titles-only      Extract from title only (no per-page HTML fetch; much faster)
    --force            Re-process URLs already recorded in scrape_state

Progress is checkpointed in scrape_state.metadata by listing-page number so
the script can be safely interrupted and resumed.

Note: Without --titles-only, each press release requires an additional HTTP
request (~1 req/sec). Full 2015–present run is ~40K+ releases and will take
several hours. Use --titles-only for a quick first pass.
"""

import argparse
import json
import sys
import time
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pubrecsearch import db, r2
from pubrecsearch.db import sha256, insert_document, bulk_insert_individuals, get_conn
from pubrecsearch.monitoring import configure_logging, get_logger
from pubrecsearch.normalize import normalize_name
from pubrecsearch.scrapers.doj_press import (
    DojPressScraper,
    extract_individuals,
    fetch_listing_page,
    _extract_body_text,
    _extract_title,
    _fetch_url,
)

log = get_logger("doj_bootstrap")
SOURCE_ID = "doj_press"
_LISTING_BASE = "https://www.justice.gov/news/press-releases"


def _parse_date(s: str) -> date | None:
    """Parse a date string like 'April 27, 2026' or '2026-04-27'."""
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def run(
    from_date: date,
    to_date: date,
    max_pages: int,
    titles_only: bool,
    force: bool,
) -> None:
    state = db.get_state(SOURCE_ID)
    meta = state.get("metadata") or {}
    processed_urls: set = set(meta.get("bootstrap_processed_urls", []))

    scraper = DojPressScraper()
    total_processed = 0
    total_new = 0

    for page in range(0, max_pages):
        log.info("listing_page", page=page, processed_so_far=total_processed)

        items = fetch_listing_page(page)
        if not items:
            log.info("no_items_on_page", page=page)
            break

        # Detect if we've gone past from_date
        page_dates = []
        for item in items:
            d = _parse_date(item.get("date_str", ""))
            if d:
                page_dates.append(d)

        if page_dates and max(page_dates) < from_date:
            log.info("passed_from_date", page=page, oldest=min(page_dates).isoformat())
            break

        rows: list[dict] = []

        for item in items:
            url = item["url"]
            item_title = item["title"]
            item_date = _parse_date(item.get("date_str", "")) or to_date

            # Skip if outside date range
            if item_date < from_date or item_date > to_date:
                continue

            if url in processed_urls and not force:
                continue

            pub_date = item_date.isoformat()

            if titles_only:
                # Fast path: use title regex only, no full page fetch
                individuals = extract_individuals(item_title, "", url, pub_date)
                for ind in individuals:
                    nrm = normalize_name(ind.name)
                    if nrm:
                        rows.append({
                            "name": ind.name,
                            "name_norm": nrm,
                            "excerpt": ind.excerpt or "",
                            "identifiers": ind.identifiers or {},
                        })
                processed_urls.add(url)
                total_processed += 1
            else:
                # Full path: fetch HTML, store in R2, extract from full page
                try:
                    raw_html = _fetch_url(url, timeout=45)
                except Exception as exc:
                    log.warning("fetch_failed", url=url, error=str(exc))
                    time.sleep(2)
                    continue

                file_hash = sha256(raw_html)
                slug = url.rstrip("/").split("/")[-1]
                filename = f"doj_{slug}.html"
                key = r2.r2_key(SOURCE_ID, pub_date[:7], filename)  # group by month

                if not db.document_exists(file_hash):
                    try:
                        r2.upload(key, raw_html, "text/html")
                    except Exception as exc:
                        log.warning("r2_upload_failed", url=url, error=str(exc))

                    doc_id = insert_document(
                        r2_key=key,
                        source_id=SOURCE_ID,
                        source_url=url,
                        file_hash=file_hash,
                        file_size=len(raw_html),
                        doc_type="html",
                        period=pub_date[:7],  # YYYY-MM
                    )

                    html_title = _extract_title(raw_html) or item_title
                    body_text = _extract_body_text(raw_html)
                    individuals = extract_individuals(html_title, body_text, url, pub_date)

                    with get_conn() as conn:
                        batch = [
                            {
                                "name": ind.name,
                                "name_norm": normalize_name(ind.name),
                                "excerpt": ind.excerpt or "",
                                "identifiers": ind.identifiers or {},
                            }
                            for ind in individuals
                            if normalize_name(ind.name)
                        ]
                        if batch:
                            ins, tot = bulk_insert_individuals(
                                conn, doc_id, "doj_defendant", batch
                            )
                            total_new += ins
                            total_processed += tot

                processed_urls.add(url)
                time.sleep(1.1)  # respect DOJ rate limit

        # For titles-only mode, do a single bulk insert per page
        if titles_only and rows:
            # Store a synthetic "listing page" document
            page_json = json.dumps(items).encode()
            file_hash = sha256(page_json)
            key = r2.r2_key(SOURCE_ID, "listing", f"doj_listing_page_{page}.json")
            if not db.document_exists(file_hash):
                r2.upload(key, page_json, "application/json")
            doc_id = insert_document(
                r2_key=key,
                source_id=SOURCE_ID,
                source_url=f"{_LISTING_BASE}?page={page}",
                file_hash=file_hash,
                file_size=len(page_json),
                doc_type="json",
                period=f"listing-p{page}",
            )
            with get_conn() as conn:
                ins, tot = bulk_insert_individuals(conn, doc_id, "doj_defendant", rows)
                total_new += ins
                total_processed += tot

        # Checkpoint every 10 pages
        if page % 10 == 0:
            meta["bootstrap_processed_urls"] = list(processed_urls)
            db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
            log.info(
                "checkpoint", page=page,
                total_processed=total_processed, total_new=total_new,
            )
            time.sleep(0.5)

    meta["bootstrap_processed_urls"] = list(processed_urls)
    db.set_state(SOURCE_ID, cursor=state.get("cursor"), metadata=meta)
    log.info("bootstrap_complete", total_processed=total_processed, total_new=total_new)


def main() -> None:
    configure_logging("INFO")
    parser = argparse.ArgumentParser(description="DOJ Press Releases historical bootstrap")
    parser.add_argument("--from-date", default="2015-01-01")
    parser.add_argument("--to-date", default=date.today().isoformat())
    parser.add_argument(
        "--max-pages", type=int, default=5000,
        help="Max listing pages to walk (default: 5000 ≈ full 2015–present)",
    )
    parser.add_argument(
        "--titles-only", action="store_true",
        help="Extract from listing-page titles only (fast; lower recall for body patterns)",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    from_d = date.fromisoformat(args.from_date)
    to_d = date.fromisoformat(args.to_date)

    log.info(
        "bootstrap_start",
        from_date=from_d.isoformat(),
        to_date=to_d.isoformat(),
        max_pages=args.max_pages,
        titles_only=args.titles_only,
    )
    run(from_d, to_d, args.max_pages, args.titles_only, args.force)


if __name__ == "__main__":
    main()
