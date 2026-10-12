from __future__ import annotations

import hashlib
import json
import re
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class StrategyError(ValueError):
    pass


@dataclass(frozen=True)
class Strategy:
    strategy_id: str
    version: str
    status: str
    universe: tuple[str, ...]
    bar_interval_minutes: int
    opening_range_minutes: int
    entry_window_et: dict[str, str]
    rules: dict[str, Any]
    risk: dict[str, Any]
    execution: dict[str, Any]
    configuration: dict[str, Any]
    sha256: str
    filename: str


_TOP = {"strategy_id", "version", "status", "universe", "bar_interval_minutes", "opening_range_minutes", "entry_window_et", "rules", "risk", "execution"}
_RULES = {"trend_ema_fast", "trend_ema_slow", "relative_volume_min", "atr_period", "max_atr_fraction", "breakout_buffer_atr",
          "min_dte", "max_dte", "min_abs_delta", "max_abs_delta", "target_abs_delta", "max_spread_fraction", "max_spread_dollars",
          "min_open_interest", "min_option_volume", "max_contracts", "stop_underlying_atr", "target_atr_multiple", "max_holding_minutes"}
_RISK = {"max_risk_per_trade_fraction", "hard_max_risk_per_trade_fraction", "max_daily_loss_fraction", "max_weekly_loss_fraction",
         "max_consecutive_losses", "max_trades_per_day", "max_open_positions"}
_EXEC = {"paper_buy_price", "paper_sell_price", "slippage_per_contract", "commission_per_contract", "exchange_fee_per_contract", "min_expected_net_r", "mode"}


def _strip_json_comments(source: str) -> str:
    """Strip // and /* */ comments without touching strings."""
    out: list[str] = []
    i, in_string, escaped = 0, False, False
    while i < len(source):
        char = source[i]
        if in_string:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
        elif source.startswith("//", i):
            i = source.find("\n", i)
            if i < 0:
                break
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise StrategyError("Unclosed block comment")
            out.extend("\n" for c in source[i:end + 2] if c == "\n")
            i = end + 2
        else:
            out.append(char)
            i += 1
    if in_string:
        raise StrategyError("Unclosed string")
    return "".join(out)


def parse_strategy(path: str | Path) -> Strategy:
    file = Path(path)
    raw = file.read_text(encoding="utf-8")
    try:
        data = json.loads(_strip_json_comments(raw), parse_constant=lambda token: (_ for _ in ()).throw(StrategyError(f"Invalid number {token}")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StrategyError(f"Invalid strategy file: {exc}") from exc
    if not isinstance(data, dict):
        raise StrategyError("Strategy root must be an object")
    _keys(data, _TOP, "strategy")
    for section, allowed in (("rules", _RULES), ("risk", _RISK), ("execution", _EXEC)):
        if not isinstance(data.get(section), dict):
            raise StrategyError(f"{section} must be an object")
        _keys(data[section], allowed, section)
    required = {
        "strategy_id": str, "version": str, "status": str, "universe": list,
        "bar_interval_minutes": int, "opening_range_minutes": int,
        "entry_window_et": dict,
    }
    for key, kind in required.items():
        if key not in data or not isinstance(data[key], kind):
            raise StrategyError(f"Missing or invalid {key}")
    if not data["universe"] or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z.]{1,8}", s) for s in data["universe"]):
        raise StrategyError("universe must contain uppercase ticker symbols")
    if not data["version"].strip():
        raise StrategyError("version must be non-empty")
    if not data["strategy_id"].strip() or data["status"] != "research_test":
        raise StrategyError("strategy_id must be non-empty and status must remain research_test")
    try:
        for value in data["entry_window_et"].values():
            datetime.strptime(value, "%H:%M")
    except (TypeError, ValueError):
        raise StrategyError("entry_window_et must define valid HH:MM start and end")
    if set(data["entry_window_et"]) != {"start", "end"}:
        raise StrategyError("entry_window_et must define HH:MM start and end")
    rules, risk, execution = data["rules"], data["risk"], data["execution"]
    for key in _RULES:
        if key not in rules or not isinstance(rules[key], (float, int)) or isinstance(rules[key], bool):
            raise StrategyError(f"Missing or invalid rules.{key}")
    for key in _RISK:
        if key not in risk or not isinstance(risk[key], (float, int)) or isinstance(risk[key], bool):
            raise StrategyError(f"Missing or invalid risk.{key}")
    for key in _EXEC:
        if key not in execution:
            raise StrategyError(f"Missing execution.{key}")
    for section_name, section in (("rules", rules), ("risk", risk)):
        for key, value in section.items():
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise StrategyError(f"{section_name}.{key} must be finite")
    for key in ("trend_ema_fast", "trend_ema_slow", "atr_period", "min_dte", "max_dte", "min_open_interest",
                "min_option_volume", "max_contracts", "max_holding_minutes"):
        if rules[key] < 1 or not float(rules[key]).is_integer():
            raise StrategyError(f"rules.{key} must be a positive integer")
    if rules["trend_ema_fast"] >= rules["trend_ema_slow"] or rules["min_dte"] > rules["max_dte"]:
        raise StrategyError("EMA periods and DTE range are inconsistent")
    if (rules["min_dte"] < 1 or not 0 < rules["min_abs_delta"] <= rules["target_abs_delta"] <= rules["max_abs_delta"] <= 1
            or rules["max_spread_fraction"] >= 1 or rules["max_spread_dollars"] <= 0):
        raise StrategyError("DTE, delta, and spread bounds are invalid")
    if not 0 < risk["max_risk_per_trade_fraction"] <= risk["hard_max_risk_per_trade_fraction"] <= .02:
        raise StrategyError("Per-trade risk must be positive and hard capped at 2%")
    if not 0 < risk["max_daily_loss_fraction"] <= .02 or not 0 < risk["max_weekly_loss_fraction"] <= .05:
        raise StrategyError("Daily/weekly loss limits exceed hard defaults")
    for key in ("max_consecutive_losses", "max_trades_per_day", "max_open_positions"):
        if risk[key] < 1 or not float(risk[key]).is_integer():
            raise StrategyError(f"risk.{key} must be a positive integer")
    for key in ("max_atr_fraction", "relative_volume_min", "max_spread_fraction", "max_spread_dollars", "target_atr_multiple", "max_contracts", "max_holding_minutes"):
        if rules[key] <= 0:
            raise StrategyError(f"rules.{key} must be positive")
    if execution["mode"] != "PAPER":
        raise StrategyError("Active strategy execution.mode must remain PAPER")
    if execution["paper_buy_price"] not in {"ask", "mid"} or execution["paper_sell_price"] not in {"bid", "mid"}:
        raise StrategyError("Paper fill model must use ask/mid for buys and bid/mid for sells")
    for key in ("slippage_per_contract", "commission_per_contract", "exchange_fee_per_contract", "min_expected_net_r"):
        if not isinstance(execution[key], (int, float)) or isinstance(execution[key], bool) or not math.isfinite(execution[key]) or execution[key] < 0:
            raise StrategyError(f"execution.{key} must be a finite nonnegative number")
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Strategy(data["strategy_id"], data["version"], data["status"], tuple(data["universe"]),
                    data["bar_interval_minutes"], data["opening_range_minutes"], data["entry_window_et"],
                    rules, risk, execution, data, config_hash, file.name)


def _keys(value: dict[str, Any], allowed: set[str], name: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise StrategyError(f"Unknown {name} keys: {', '.join(sorted(unknown))}")


def write_manifest(strategy: Strategy, path: str | Path, deployment: dict[str, Any] | None = None) -> None:
    payload = {"strategy_id": strategy.strategy_id, "version": strategy.version, "status": strategy.status,
               "filename": strategy.filename, "sha256": strategy.sha256, "configuration_hash": strategy.sha256,
               "active_strategy_version": strategy.version,
               "activated_at": datetime.now(timezone.utc).isoformat(), "configuration": strategy.configuration,
               "deployment_metadata": deployment or {"status": "research_test", "mode": "PAPER", "live_enabled": False}}
    _atomic_json(Path(path), payload)


def verify_manifest(strategy_path: str | Path, manifest_path: str | Path) -> dict[str, Any]:
    strategy = parse_strategy(strategy_path)
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if (manifest.get("sha256") != strategy.sha256 or manifest.get("configuration_hash") != strategy.sha256
            or manifest.get("filename") != strategy.filename or manifest.get("version") != strategy.version
            or manifest.get("active_strategy_version") != strategy.version
            or manifest.get("strategy_id") != strategy.strategy_id or manifest.get("status") != "research_test"
            or manifest.get("configuration") != strategy.configuration
            or (manifest.get("deployment_metadata") or {}).get("mode") != "PAPER"
            or (manifest.get("deployment_metadata") or {}).get("live_enabled") is not False):
        raise StrategyError("Active strategy manifest hash/version mismatch")
    return manifest


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)
