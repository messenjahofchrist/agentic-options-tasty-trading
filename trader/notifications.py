from __future__ import annotations

import os
import smtplib
import urllib.request
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


# Events that also go to the phone. The rest (stale quotes, a symbol switched off) are email/log only,
# because they happen many times a day and a push for each would bury the ones that matter.
PUSH_EVENTS = frozenset({
    "paper_engine_runtime_failure", "paper_position_protective_exit",
    "paper_position_unresolved_at_session_close", "live_position_unresolved_at_session_close",
})


def push_configured() -> bool:
    return bool(os.getenv("NTFY_URL", "").strip())


def send_push(title: str, message: str, *, priority: str = "high", tags: str = "warning") -> None:
    """Phone push through ntfy (NTFY_URL = https://ntfy.sh/<private topic>, or a self-hosted server).
    priority "urgent" is ntfy's max level, which the app can let through Do Not Disturb."""
    if not push_configured():
        raise RuntimeError("Push is not configured; set NTFY_URL")
    headers = {"Title": title.encode("ascii", "replace").decode(), "Priority": priority, "Tags": tags}
    token = os.getenv("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(os.environ["NTFY_URL"].strip(), data=message.encode("utf-8"),
                                     headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def ping_dead_man(*, failed: bool = False, detail: str = "") -> str:
    """Dead-man's-switch ping (HEALTHCHECK_URL, e.g. a healthchecks.io check). The external service alerts
    when pings stop, which covers what no local monitor can report: the PC asleep, offline or powered off."""
    url = os.getenv("HEALTHCHECK_URL", "").strip()
    if not url:
        return "not_configured"
    try:
        request = urllib.request.Request(url.rstrip("/") + ("/fail" if failed else ""),
                                         data=detail[:1000].encode("utf-8"), method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        return "sent"
    except OSError as exc:
        return f"failed: {type(exc).__name__}"


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
    if event in PUSH_EVENTS:
        record["push_status"] = "not_configured"
        if push_configured():
            try:
                summary = ", ".join(f"{key}={value}" for key, value in details.items()
                                    if key in ("underlying", "option_symbol", "realized_pnl", "error_type", "error"))
                send_push(f"OptioBot {mode}: {event.replace('_', ' ')}", summary[:400] or event, priority="urgent")
                record["push_status"] = "sent"
            except (OSError, RuntimeError, ValueError) as exc:
                record["push_status"] = f"failed: {type(exc).__name__}"
    append_jsonl(data_dir / "critical_alerts.jsonl", record)
    return record
