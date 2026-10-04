from __future__ import annotations

import uuid

from .config import AppConfig, Mode
from .tastytrade_client import TastytradeClient


class LiveExecution:
    """Explicitly gated LIVE order adapter. It always dry-runs and never auto-retries."""
    def __init__(self, config: AppConfig, client: TastytradeClient):
        config.validate_startup()
        if config.mode is not Mode.LIVE:
            raise RuntimeError("Live execution is only available in explicitly enabled LIVE mode")
        self.config, self.client = config, client

    def place(self, order: dict) -> dict:
        body = dict(order)
        body.setdefault("external-identifier", str(uuid.uuid4()))
        preflight = self.client.dry_run_order(self.config.account_number, body)
        data = preflight.get("data") or {}
        if data.get("errors") or data.get("warnings"):
            raise RuntimeError("LIVE blocked: dry-run returned errors or warnings; review preflight manually")
        if not data.get("order") and not data.get("complex-order"):
            raise RuntimeError("LIVE blocked: dry-run did not validate an order")
        # The production caller still needs independent position/account reconciliation and verified protection.
        return self.client.submit_order(self.config.account_number, body, dry_run_result=preflight)
