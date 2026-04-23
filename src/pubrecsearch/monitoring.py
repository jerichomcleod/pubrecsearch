"""Structured logging and email alert utilities."""

import logging
import sys
from typing import Any

import structlog

from .config import get_settings


def configure_logging(log_level: str = "INFO") -> None:
    """Set up structlog with JSON output suitable for log rotation."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper(), logging.INFO),
    )


def get_logger(source_id: str | None = None) -> structlog.stdlib.BoundLogger:
    log = structlog.get_logger()
    if source_id:
        log = log.bind(source_id=source_id)
    return log


# ---------------------------------------------------------------------------
# Email alerts via Resend
# ---------------------------------------------------------------------------


def send_alert(subject: str, body: str) -> None:
    """Send an email alert. Swallows errors so a failed alert never kills a job."""
    settings = get_settings()
    if not settings.resend_api_key or not settings.alert_email:
        return

    try:
        import resend

        resend.api_key = settings.resend_api_key
        resend.Emails.send(
            {
                "from": "pubrecsearch-alerts@resend.dev",
                "to": [settings.alert_email],
                "subject": subject,
                "text": body,
            }
        )
    except Exception as exc:
        structlog.get_logger().warning("alert_send_failed", error=str(exc))


def send_job_failure_alert(source_id: str, job_id: int, error_summary: str) -> None:
    send_alert(
        subject=f"[PubRecSearch] FAILED: {source_id}",
        body=(
            f"Scrape job failed.\n\n"
            f"Source:  {source_id}\n"
            f"Job ID:  {job_id}\n"
            f"Summary: {error_summary}\n"
        ),
    )


def send_daily_heartbeat(source_summaries: list[dict[str, Any]]) -> None:
    """Send a daily summary email listing the status of all recent jobs."""
    lines = ["PubRecSearch daily scrape summary\n"]
    for s in source_summaries:
        status_icon = "✓" if s.get("status") == "success" else "✗"
        lines.append(
            f"  {status_icon}  {s['source_id']:30s}  {s.get('status','?'):10s}  "
            f"new={s.get('records_new', 0):>8,}  errors={s.get('errors_count', 0):>4}"
        )
    send_alert(subject="[PubRecSearch] Daily heartbeat", body="\n".join(lines))
