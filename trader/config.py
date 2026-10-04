from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True)
class AppConfig:
    mode: Mode = Mode.PAPER
    data_dir: Path = Path("data")
    starting_cash: float = 10_000.0
    live_enabled: bool = False
    live_confirmation: str = ""
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""
    account_number: str = ""
    production_base_url: str = "https://api.tastyworks.com"

    @classmethod
    def from_env(cls) -> "AppConfig":
        mode_raw = os.getenv("TRADING_MODE", "PAPER").upper()
        try:
            mode = Mode(mode_raw)
        except ValueError as exc:
            raise ValueError(f"Unsupported TRADING_MODE: {mode_raw}") from exc
        return cls(
            mode=mode,
            data_dir=Path(os.getenv("OPTIOBOT_DATA_DIR", "data")),
            starting_cash=float(os.getenv("PAPER_STARTING_CASH", "10000")),
            live_enabled=os.getenv("ENABLE_LIVE_TRADING", "false").lower() == "true",
            live_confirmation=os.getenv("LIVE_START_CONFIRMATION", ""),
            client_id=os.getenv("TASTYTRADE_CLIENT_ID", ""),
            client_secret=os.getenv("TASTYTRADE_CLIENT_SECRET", ""),
            refresh_token=os.getenv("TASTYTRADE_REFRESH_TOKEN", ""),
            account_number=os.getenv("TASTYTRADE_ACCOUNT_NUMBER", ""),
        )

    def validate_startup(self) -> None:
        if self.starting_cash <= 0:
            raise ValueError("PAPER_STARTING_CASH must be positive")
        if self.mode == Mode.LIVE:
            if not self.live_enabled or self.live_confirmation != "I UNDERSTAND LIVE TRADING USES REAL MONEY":
                raise RuntimeError("LIVE blocked: explicit enable flag and startup confirmation are required")
            missing = [name for name, value in (("client id", self.client_id), ("client secret", self.client_secret),
                       ("refresh token", self.refresh_token), ("account number", self.account_number)) if not value]
            if missing:
                raise RuntimeError("LIVE blocked: missing " + ", ".join(missing))
