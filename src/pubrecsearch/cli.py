"""CLI entry point: pubrecsearch <command>"""

import click

from .monitoring import configure_logging


@click.group()
@click.option("--log-level", default="INFO", envvar="LOG_LEVEL")
def cli(log_level: str):
    configure_logging(log_level)


@cli.command("run")
@click.argument("source_id", required=False)
def run_cmd(source_id: str | None):
    """Run one or all scrapers immediately (outside the scheduler)."""
    from .runner import ScraperRunner
    from .scrapers import ALL_SCRAPERS

    scrapers = (
        [s for s in ALL_SCRAPERS if s.source_id == source_id]
        if source_id
        else ALL_SCRAPERS
    )

    if not scrapers:
        raise click.ClickException(f"Unknown source_id: {source_id}")

    for scraper in scrapers:
        runner = ScraperRunner(scraper)
        result = runner.run()
        click.echo(
            f"{scraper.source_id}: {result['status']} "
            f"(processed={result.get('records_processed', 0)}, "
            f"new={result.get('records_new', 0)}, "
            f"errors={result.get('errors_count', 0)})"
        )


@cli.command("schedule")
def schedule_cmd():
    """Start the APScheduler daemon (runs scrapers on their cron schedules)."""
    from .runner import build_scheduler
    from .scrapers import ALL_SCRAPERS

    scheduler = build_scheduler(ALL_SCRAPERS)
    click.echo("Scheduler started. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        click.echo("Scheduler stopped.")


@cli.command("serve")
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
@click.option("--reload", is_flag=True)
def serve_cmd(host: str, port: int, reload: bool):
    """Start the FastAPI query server."""
    import uvicorn
    uvicorn.run(
        "pubrecsearch.api.main:app",
        host=host,
        port=port,
        reload=reload,
    )


@cli.command("init-db")
def init_db_cmd():
    """Apply db/schema.sql to the configured PostgreSQL database."""
    import pathlib
    from .db import get_conn

    schema = pathlib.Path(__file__).parent.parent.parent / "db" / "schema.sql"
    if not schema.exists():
        raise click.ClickException(f"Schema file not found: {schema}")

    sql = schema.read_text()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
    click.echo("Database schema applied successfully.")
