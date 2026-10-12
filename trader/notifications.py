from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from .logging_utils import append_jsonl


def email_configured() -> bool:
    required = ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "REPORT_FROM", "REPORT_TO")
    return all(os.getenv(name, "").strip() for name in required)


def send_email(subject: str, body: str) -> None:
    if not email_configured():
        raise RuntimeError("Email is not configured; set SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, REPORT_FROM, REPORT_TO")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ["REPORT_FROM"]
    message["To"] = os.environ["REPORT_TO"]
    message.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=20) as server:
        server.starttls()
        server.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
        server.send_message(message)


def record_critical_alert(event: str, *, mode: str, details: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    """Persist a redacted deterministic alert and make one best-effort email attempt."""
    record: dict[str, Any] = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                              "severity": "critical", "event": event, "mode": mode,
                              "details": details, "email_status": "not_configured"}
    if email_configured():
        try:
            send_email(f"CRITICAL: OptioBot {event}",
                       "A deterministic safety monitor generated an alert.\n\n" +
                       "UTC: " + record["timestamp_utc"] + "\nMode: " + mode + "\nEvent: " + event +
                       "\nDetails: " + str(details))
            record["email_status"] = "sent"
        except (OSError, RuntimeError, ValueError, smtplib.SMTPException) as exc:
            record["email_status"] = "failed"
            record["email_error"] = f"{type(exc).__name__}: {exc}"
    append_jsonl(data_dir / "critical_alerts.jsonl", record)
    return record
