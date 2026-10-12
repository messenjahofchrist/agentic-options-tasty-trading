from __future__ import annotations

import os
import re
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
    massive_api_key: str = ""

    @classmethod
    def from_env(cls) -> "AppConfig":
        load_dotenv()
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
            massive_api_key=os.getenv("MASSIVE_API_KEY", ""),
        )

    def require_market_data_credentials(self) -> None:
        missing = [name for name, value in (("TASTYTRADE_CLIENT_ID", self.client_id),
                   ("TASTYTRADE_CLIENT_SECRET", self.client_secret),
                   ("TASTYTRADE_REFRESH_TOKEN", self.refresh_token)) if not value]
        if missing:
            raise RuntimeError("Missing " + ", ".join(missing) + ". Put them in the ignored local .env file; never paste credentials into chat.")

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


def load_dotenv(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE entries without overriding already-set process variables."""
    file = Path(path)
    if not file.is_file():
        return
    for number, raw in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ValueError(f"Invalid .env entry on line {number}; expected KEY=VALUE")
        key, value = (part.strip() for part in line.split("=", 1))
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"Invalid .env variable name on line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
