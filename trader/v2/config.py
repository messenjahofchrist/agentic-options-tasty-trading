"""Load and validate strategies/professional_v2.json and its activation manifest. Fails closed: unknown keys,
missing keys, wrong types, a base-strategy hash that differs from strategy.txt, or a manifest that does not match
the file all raise V2ConfigError."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from ..strategy_loader import Strategy, _strip_json_comments, parse_strategy

CONFIG_FILE = "strategies/professional_v2.json"
MANIFEST_FILE = "strategies/professional_v2.manifest.json"
REGIMES = ("STRONG_BULLISH", "WEAK_BULLISH", "STRONG_BEARISH", "WEAK_BEARISH", "RANGE_BOUND", "HIGH_VOLATILITY", "UNCLEAR")
PROFILES = ("ORIGINAL_ACCOUNT_PAPER", "SMALL_ACCOUNT_PAPER")


class V2ConfigError(ValueError):
    pass


NUM = (int, float)


def _num(lo: float | None = None, hi: float | None = None, *, integer: bool = False, lo_open: bool = False):
    def check(value: Any, where: str) -> None:
        if isinstance(value, bool) or not isinstance(value, NUM) or not math.isfinite(value):
            raise V2ConfigError(f"{where} must be a finite number")
        if integer and not float(value).is_integer():
            raise V2ConfigError(f"{where} must be an integer")
        if lo is not None and (value <= lo if lo_open else value < lo):
            raise V2ConfigError(f"{where} must be {'>' if lo_open else '>='} {lo}")
        if hi is not None and value > hi:
            raise V2ConfigError(f"{where} must be <= {hi}")
    return check


def _bool(value: Any, where: str) -> None:
    if not isinstance(value, bool):
        raise V2ConfigError(f"{where} must be true or false")


def _hhmm(value: Any, where: str) -> None:
    try:
        time.fromisoformat(value)
    except (TypeError, ValueError):
        raise V2ConfigError(f"{where} must be HH:MM") from None


def _choice(*options: str):
    def check(value: Any, where: str) -> None:
        if value not in options:
            raise V2ConfigError(f"{where} must be one of {', '.join(options)}")
    return check


def _tickers(value: Any, where: str) -> None:
    if not isinstance(value, list) or not value or any(not isinstance(s, str) or not s.isupper() or len(s) > 8 for s in value):
        raise V2ConfigError(f"{where} must be a non-empty list of uppercase tickers")


def _string(value: Any, where: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise V2ConfigError(f"{where} must be a non-empty string")


def _directions(value: Any, where: str) -> None:
    if not isinstance(value, dict) or set(value) != set(REGIMES):
        raise V2ConfigError(f"{where} must define exactly {', '.join(REGIMES)}")
    for regime, sides in value.items():
        if not isinstance(sides, list) or any(side not in ("CALL", "PUT") for side in sides):
            raise V2ConfigError(f"{where}.{regime} must be a list of CALL/PUT")


EVENT_TYPES = ("FOMC_RATE_DECISION", "CPI", "PPI", "EMPLOYMENT_SITUATION", "GDP", "PCE")


def _event_types(value: Any, where: str) -> None:
    if not isinstance(value, list) or any(item not in EVENT_TYPES for item in value):
        raise V2ConfigError(f"{where} must list event types from {', '.join(EVENT_TYPES)}")


def _fractions_by_profile(value: Any, where: str) -> None:
    if not isinstance(value, dict) or set(value) != set(PROFILES):
        raise V2ConfigError(f"{where} must define exactly {', '.join(PROFILES)}")
    for key, item in value.items():
        _num(0, 1, lo_open=True)(item, f"{where}.{key}")


SCHEMA: dict[str, Any] = {
    "strategy_id": _string, "version": _string, "status": _choice("research_test"), "mode": _choice("PAPER"),
    "base_strategy": {"filename": _string, "strategy_id": _string, "version": _string, "configuration_hash": _string},
    "sector_map_file": _string, "economic_calendar_file": _string,
    "context": {"broad_symbols": _tickers, "sector_etfs": _tickers, "volatility_index": _string,
                "max_bar_age_seconds": _num(60, 900), "vix_max_age_seconds": _num(30, 900),
                "daily_history_sessions": _num(20, 250, integer=True), "premarket_start_et": _hhmm},
    "regime": {"enabled": _bool, "vix_disorderly_level": _num(0, lo_open=True),
               "vix_disorderly_change_fraction": _num(0, 1, lo_open=True),
               "spy_disorderly_atr_fraction": _num(0, 1, lo_open=True),
               "strong_min_index_bias": _num(1, 3, integer=True), "strong_min_spy_efficiency": _num(0, 1),
               "strong_min_breadth": _num(0, 1), "weak_min_market_score": _num(1, 9, integer=True),
               "weak_min_spy_bias": _num(0, 3, integer=True), "efficiency_window_bars": _num(5, 120, integer=True),
               "allowed_directions": _directions},
    "sector": {"enabled": _bool, "min_sector_bias": _num(-3, 3, integer=True)},
    "vwap_alignment": {"enabled": _bool},
    "confirmation": {"enabled": _bool, "confirm_closes": _num(1, 10, integer=True), "min_distance_atr": _num(0, 10),
                     "max_wait_bars": _num(1, 60, integer=True),
                     "pullback": {"enabled": _bool, "retest_tolerance_atr": _num(0, 10),
                                  "max_wait_bars": _num(1, 120, integer=True)}},
    "correlation": {"enabled": _bool, "lookback_sessions": _num(10, 250, integer=True),
                    "min_observations": _num(10, 250, integer=True), "threshold": _num(0, 1, lo_open=True),
                    "fallback": _choice("sector_and_index_groups")},
    "portfolio": {"enabled": _bool, "aggregate_max_loss_fraction": _fractions_by_profile,
                  "cluster_max_loss": _choice("one_trade_budget")},
    "option_quality": {"enabled": _bool, "max_quote_age_seconds": _num(1, 300), "min_bid_size": _num(0, integer=True),
                       "min_ask_size": _num(0, integer=True), "max_iv_rank": _num(0, 1, lo_open=True),
                       "max_iv_to_hv30": _num(0, lo_open=True), "max_contract_iv_to_index_iv": _num(0, lo_open=True),
                       "max_metrics_age_minutes": _num(1, 1440), "missing_volatility_data": _choice("reject", "allow_flagged")},
    "events": {"enabled": _bool,
               "earnings": {"reject_if_reports_today_after_close": _bool, "reject_if_reports_next_session_before_open": _bool,
                            "reported_today_before_open": _choice("review", "reject", "allow"),
                            "unknown_time_of_day": _choice("treat_as_both"),
                            "estimated_date": _choice("apply_and_flag_unverified"),
                            "metrics_unavailable": _choice("reject", "allow_flagged")},
               "economic": {"FOMC_RATE_DECISION": {"block_entries_from_et": _hhmm, "block_entries_until_et": _hhmm,
                                                   "open_positions": _choice("review")},
                            "pre_open_release_types": lambda value, where: _event_types(value, where),
                            "pre_open_release_block_minutes_after_open": _num(0, 390, integer=True)},
               "calendar_expired": _choice("reject")},
    "sizing": {"max_contracts": _num(1, 10, integer=True), "max_fraction_of_displayed_size": _num(0, 1, lo_open=True),
               "max_fraction_of_open_interest": _num(0, 1, lo_open=True), "quality_score_affects_size": _bool},
    "management": {"breakeven": {"enabled": _bool, "trigger_r": _num(0, lo_open=True)},
                   "trailing": {"enabled": _bool, "trigger_r": _num(0, lo_open=True),
                                "atr_multiple": _choice("rules.stop_underlying_atr")},
                   "time_stop": {"enabled": _bool, "after_minutes": _num(1, 390, integer=True), "min_progress_r": _num(0, 10)},
                   "partial_profit": {"enabled": _bool, "trigger_r": _num(0, lo_open=True), "fraction": _num(0, 1, lo_open=True)}},
    "shadow_benchmark": {"enabled": _bool},
}


def _validate(data: Any, schema: dict[str, Any], where: str) -> None:
    if not isinstance(data, dict):
        raise V2ConfigError(f"{where or 'config'} must be an object")
    unknown, missing = set(data) - set(schema), set(schema) - set(data)
    if unknown:
        raise V2ConfigError(f"Unknown {where or 'config'} keys: {', '.join(sorted(unknown))}")
    if missing:
        raise V2ConfigError(f"Missing {where or 'config'} keys: {', '.join(sorted(missing))}")
    for key, rule in schema.items():
        path = f"{where}.{key}" if where else key
        if isinstance(rule, dict):
            _validate(data[key], rule, path)
        else:
            rule(data[key], path)


@dataclass(frozen=True)
class V2Config:
    data: dict[str, Any]
    sha256: str
    path: Path
    base: Strategy
    sector_map: dict[str, Any]
    calendar: dict[str, Any]

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def strategy_id(self) -> str:
        return self.data["strategy_id"]

    def section(self, name: str) -> dict[str, Any]:
        return self.data[name]

    def context_symbols(self) -> tuple[str, ...]:
        context = self.data["context"]
        return tuple(dict.fromkeys([*context["broad_symbols"], *context["sector_etfs"]]))


def _canonical_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(_strip_json_comments(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2ConfigError(f"Cannot read {path}: {exc}") from exc


def validate_sector_map(data: Any, context_sector_etfs: list[str]) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("symbols"), dict) or not isinstance(data.get("sector_etfs"), dict):
        raise V2ConfigError("sector map needs 'symbols' and 'sector_etfs' objects")
    for etf in data["sector_etfs"]:
        if etf not in context_sector_etfs:
            raise V2ConfigError(f"sector ETF {etf} is mapped but not streamed as market context")
    groups = {"us_equity", "china_adr", "sector_etf", "broad_index", "rates", "commodity", "international", "crypto"}
    for symbol, entry in data["symbols"].items():
        if not isinstance(entry, dict) or entry.get("group") not in groups:
            raise V2ConfigError(f"sector map {symbol}: group must be one of {', '.join(sorted(groups))}")
        etf = entry.get("sector_etf")
        if etf is not None and etf not in data["sector_etfs"]:
            raise V2ConfigError(f"sector map {symbol}: unknown sector ETF {etf}")
        if entry["group"] in {"us_equity", "china_adr", "sector_etf"} and etf is None:
            raise V2ConfigError(f"sector map {symbol}: an equity needs a sector ETF")


def validate_calendar(data: Any) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) or not isinstance(data.get("sources"), dict):
        raise V2ConfigError("economic calendar needs 'events' and 'sources'")
    try:
        date.fromisoformat(data["calendar_valid_through"])
        datetime.fromisoformat(data["verified_at_utc"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise V2ConfigError("economic calendar needs calendar_valid_through (date) and verified_at_utc") from None
    for number, event in enumerate(data["events"]):
        try:
            date.fromisoformat(event["date"]); time.fromisoformat(event["time_et"])
        except (KeyError, TypeError, ValueError):
            raise V2ConfigError(f"calendar event {number} needs a date and HH:MM time_et") from None
        if (event.get("source") not in data["sources"] or not isinstance(event.get("time_verified"), bool)
                or event.get("type") not in EVENT_TYPES):
            raise V2ConfigError(f"calendar event {number} needs a known source, type and time_verified")


def load_v2_config(root: Path, path: str | Path = CONFIG_FILE) -> V2Config:
    file = Path(path) if Path(path).is_absolute() else root / path
    data = _load_json(file)
    _validate(data, SCHEMA, "")
    base_info = data["base_strategy"]
    base = parse_strategy(root / base_info["filename"])
    if (base.sha256 != base_info["configuration_hash"] or base.strategy_id != base_info["strategy_id"]
            or base.version != base_info["version"]):
        raise V2ConfigError(f"V2 is pinned to base strategy {base_info['version']} {base_info['configuration_hash'][:12]}, "
                            f"but {base_info['filename']} is {base.version} {base.sha256[:12]}")
    if data["correlation"]["min_observations"] > data["correlation"]["lookback_sessions"]:
        raise V2ConfigError("correlation.min_observations cannot exceed lookback_sessions")
    fomc = data["events"]["economic"]["FOMC_RATE_DECISION"]
    if time.fromisoformat(fomc["block_entries_from_et"]) >= time.fromisoformat(fomc["block_entries_until_et"]):
        raise V2ConfigError("FOMC block window must start before it ends")
    sector_map = _load_json(root / data["sector_map_file"])
    validate_sector_map(sector_map, data["context"]["sector_etfs"])
    calendar = _load_json(root / data["economic_calendar_file"])
    validate_calendar(calendar)
    combined = {"config": data, "sector_map": sector_map, "calendar": calendar}
    return V2Config(data, _canonical_hash(combined), file, base, sector_map, calendar)


def write_v2_manifest(root: Path, config: V2Config, *, approved_by: str, note: str) -> Path:
    path = root / MANIFEST_FILE
    payload = {"strategy_id": config.strategy_id, "version": config.version, "status": "research_test", "mode": "PAPER",
               "live_enabled": False, "configuration_hash": config.sha256,
               "base_strategy_hash": config.base.sha256, "base_strategy_version": config.base.version,
               "activated_at_utc": datetime.now(timezone.utc).isoformat(), "approved_by": approved_by, "note": note}
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def verify_v2_manifest(root: Path, config: V2Config) -> dict[str, Any]:
    path = root / MANIFEST_FILE
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise V2ConfigError(f"V2 is not activated ({path.name} missing or unreadable): {exc}") from exc
    if (manifest.get("configuration_hash") != config.sha256 or manifest.get("base_strategy_hash") != config.base.sha256
            or manifest.get("version") != config.version or manifest.get("mode") != "PAPER"
            or manifest.get("live_enabled") is not False or manifest.get("status") != "research_test"):
        raise V2ConfigError("V2 manifest does not match the current V2 configuration (re-run v2-activate after review)")
    return manifest
