"""A V2 version run BESIDE the active one, on its own PAPER ledgers (owner, 2026-11-12: "create 2.9 separately",
"run both together and compare", "preserve the current version").

The active version (strategies/professional_v2.json + its manifest) and the original $10K / $1K ledgers are not read
or written here. The side version has its own config file and its own manifest next to it, and two ledgers:

  10k  profiles/v29_original_account.json   data/v29/original_paper        $10,000, the original account's rules
  1k   profiles/v29_small_account.json      data/v29/small_account_paper   $1,000, the $1K account's owner rules

    python -m trader.cli side-session --ledger 10k     (scheduled task \\OptioBot\\V29Engine10K)
    python -m trader.cli side-session --ledger 1k      (scheduled task \\OptioBot\\V29Engine1K)

The commands are not "paper-session", so the hourly AI reviewers (which find ledgers by that command) leave them out.
The watchdog checks every profiles/*.json ledger, so it does watch these two engines.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .in_play import SIDE_CONFIG, todays_selection

LEDGERS = {"10k": "profiles/v29_original_account.json", "1k": "profiles/v29_small_account.json"}


def ledger_dir(root: Path, ledger: str) -> Path:
    data = json.loads((root / LEDGERS[ledger]).read_text(encoding="utf-8"))
    path = Path(data["data_dir"])
    return path if path.is_absolute() else root / path


def run_side_session(*, root: Path, cfg, ledger: str, config_path: str = SIDE_CONFIG) -> dict:
    from ..config import Mode
    from ..paper_runner import _set_engine_expected, _write_heartbeat, run_paper_session
    from ..strategy_loader import parse_strategy
    from ..tastytrade_client import set_request_rate_limit
    from .config import in_play_universe, load_v2_config, side_manifest_path, verify_v2_manifest

    if cfg.mode is not Mode.PAPER:
        raise RuntimeError("a side version runs on PAPER ledgers only")
    strategy = parse_strategy(root / "strategy.txt")
    config_file = root / config_path
    v2 = load_v2_config(root, config_file)
    verify_v2_manifest(root, v2, side_manifest_path(config_file))
    if (root / "strategies" / "professional_v2.json").resolve() == config_file.resolve():
        raise RuntimeError("the side session must not run the active V2 config file")
    data_dir = ledger_dir(root, ledger)
    data_dir.mkdir(parents=True, exist_ok=True)
    universe = tuple(in_play_universe(root, v2.data)) if (v2.data.get("in_play") or {}).get("enabled") else tuple(strategy.universe)
    symbols: tuple[str, ...] | None = None
    if (v2.data.get("in_play") or {}).get("enabled"):
        _set_engine_expected(data_dir, expected=False, state="WAITING_FOR_SCAN", symbol="in_play")
        pick = todays_selection(root, cfg, v2.data, log=lambda t: print(t, flush=True))
        symbols = tuple(r["symbol"] for r in pick.get("selected") or [])
        print(f"V2 {v2.version} in play today ({pick.get('status')}): "
              + (", ".join(f"{r['symbol']} {r['relative_volume']}x" for r in pick.get("selected") or []) or "none"), flush=True)
        if not symbols:
            # Nothing to trade: say so in the heartbeat (not expected to run, so the watchdog does not alarm).
            state = "NO_STOCKS_IN_PLAY" if pick.get("status") == "ok" else "SCAN_FAILED"
            _set_engine_expected(data_dir, expected=False, state=state, symbol="in_play")
            _write_heartbeat(data_dir / "engine_heartbeat.json", status="stopped", symbol=state.lower(),
                             error=pick.get("error"))
            if pick.get("status") != "ok":
                _notify_failure(ledger, v2.version, pick.get("error") or "scan failed")
            return {"ledger": ledger, "symbols": [], "scan": pick.get("status")}
    set_request_rate_limit(10)
    if ledger == "10k":
        descriptor = json.loads((root / LEDGERS["10k"]).read_text(encoding="utf-8"))
        run_cfg = replace(cfg, data_dir=data_dir, starting_cash=float(descriptor["starting_balance"]))
        run_paper_session(root=root, cfg=run_cfg, strategy=replace(strategy, universe=universe), symbols=symbols, v2=v2)
    else:
        from ..account_profiles import run_profile_paper_session
        run_profile_paper_session(root=root, cfg=cfg, strategy=strategy, profile_name=LEDGERS["1k"], symbols=symbols, v2=v2)
    return {"ledger": ledger, "symbols": list(symbols or []), "finished_at_utc": datetime.now(timezone.utc).isoformat()}


def _notify_failure(ledger: str, version: str, error: str) -> None:
    try:
        from ..notifications import email_configured, send_email
        if email_configured():
            send_email(f"OptioBot {version} ({ledger}) did not start: stocks-in-play scan failed",
                       f"The {version} side version could not pick today's stocks: {error}\n"
                       "2.8.0 on the original ledgers is not affected.")
    except Exception:
        pass
