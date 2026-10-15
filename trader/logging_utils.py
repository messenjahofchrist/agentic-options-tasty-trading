from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def atomic_replace(src: str | Path, dst: str | Path, *, attempts: int = 10) -> None:
    """os.replace with retry. On Windows a replace fails with PermissionError (WinError 5) when any
    other process momentarily has the destination open for reading (a status check, an antivirus
    scan). The write is still valid, so retry briefly before giving up."""
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))


def get_logger(name: str = "optio_bot") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
        logger.addHandler(handler); logger.setLevel(logging.INFO)
    return logger


def append_jsonl(path: str | Path, record: dict[str, Any]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1024, int(os.getenv("OPTIOBOT_LOG_MAX_BYTES", str(10 * 1024 * 1024))))
    if file.is_file() and file.stat().st_size >= max_bytes:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        archive = file.with_name(f"{file.name}.{stamp}.{uuid.uuid4().hex[:6]}.archive")
        os.replace(file, archive)
    payload = dict(record)
    payload.setdefault("timestamp_utc", datetime.now(timezone.utc).isoformat())
    with file.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
