"""Historical replay: ORIGINAL strategy vs PROFESSIONAL_STRATEGY_V2 on the same 1-minute history, same harness.

What is replayed exactly (the production code, not a re-implementation):
  core signal and indicators (strategy_engine.evaluate_underlying_diagnostics), breakout confirmation (confirmation.py),
  market context, regime, sector confirmation, VWAP alignment, FOMC windows, correlation clusters and the aggregate
  cap (pipeline.evaluate_candidate stages 1-7), trailing stop and time stop (management.py).
What is NOT replayed (no historical data): option selection, option bid/ask, Greeks and the expected-net-R gate,
  IV rank/HV checks, earnings dates, the dollar daily-loss limit. Breakeven is approximated on the underlying (exit
  when the close returns to the entry price after arming). P&L is the underlying proxy the original backtest uses:
  100 shares, $0.01/share slippage each way, $1.40 fees per trade, R = net / (|entry - stop| x 100).
Harness rules for both strategies: completed-bar decisions, entry at that bar's close, exits on later completed bar
closes (like the live exit check), flat at the session's last bar, max 4 open / 6 entries per session (original risk
limits), no averaging, same-minute candidates ranked by trend efficiency (expected net R needs option quotes), the
3-consecutive-loss block reset each session (live carries it across sessions; resetting stops one streak from
ending the replay). No bar after the decision bar is ever used for a decision.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..models import Bar
from ..risk_engine import RiskState
from ..strategy_engine import evaluate_underlying_diagnostics
from . import management as management_mod
from . import pipeline
from .config import V2Config, load_v2_config
from .confirmation import TERMINAL, BreakoutTracker
from .context import DailyLevels, MarketContext, VixReading
from .correlation import CorrelationModel
from .events import EventEngine
from .regime import classify
from .sectors import SectorMap

ET = ZoneInfo("America/New_York")
SLIPPAGE_PER_SHARE, FEE_PER_TRADE = 0.01, 1.40
UNIT_RISK = 1000.0          # replay max loss per trade: the original $10K ledger's 10% per-trade budget
HISTORICAL_FOMC = [          # federalreserve.gov/monetarypolicy/fomccalendars.htm (2026 meetings before Oct)
    {"date": "2026-06-17", "time_et": "14:00", "time_verified": False, "type": "FOMC_RATE_DECISION", "name": "FOMC Jun 16-17", "source": "FOMC"},
    {"date": "2026-07-29", "time_et": "14:00", "time_verified": False, "type": "FOMC_RATE_DECISION", "name": "FOMC Jul 28-29", "source": "FOMC"},
    {"date": "2026-09-16", "time_et": "14:00", "time_verified": False, "type": "FOMC_RATE_DECISION", "name": "FOMC Sep 15-16", "source": "FOMC"},
]


class _NoOptionsInReplay(Exception):
    pass


def _bars(rows: list[dict]) -> list[Bar]:
    return [Bar(datetime.fromisoformat(r["timestamp"]).astimezone(timezone.utc), float(r["open"]), float(r["high"]),
                float(r["low"]), float(r["close"]), float(r.get("volume") or 0)) for r in rows]


def _split(bars: list[Bar]) -> tuple[dict[date, list[Bar]], dict[date, dict]]:
    """Regular-session bars per ET date, and the 04:00-09:30 ET premarket high/low per date."""
    rth: dict[date, list[Bar]] = defaultdict(list)
    pre: dict[date, dict] = {}
    for bar in sorted(bars, key=lambda b: b.timestamp):
        local = bar.timestamp.astimezone(ET)
        if time(9, 30) <= local.time() < time(16, 0):
            rth[local.date()].append(bar)
        elif time(4, 0) <= local.time() < time(9, 30):
            item = pre.setdefault(local.date(), {"high": bar.high, "low": bar.low, "bars": 0})
            item["high"], item["low"], item["bars"] = max(item["high"], bar.high), min(item["low"], bar.low), item["bars"] + 1
    return dict(rth), pre


def load_massive_cache(root: Path, symbols: list[str]) -> dict[str, list[Bar]]:
    out = {}
    for symbol in symbols:
        path = root / f"data/backtest_cache/massive-{symbol}-20260601T0000-20261010T0000.json"
        if path.is_file():
            out[symbol] = _bars(json.loads(path.read_text(encoding="utf-8")))
    return out


async def load_tastytrade(cfg, symbols: list[str], broad: list[str]) -> tuple[dict[str, list[Bar]], dict[str, list[Bar]], dict]:
    """Recent DXLink 1-minute history. A DXLink snapshot holds at most ~8,000 candles and a longer request comes
    back truncated (SNAPSHOT_SNIP, refused), so each series asks for the longest window that fits, stepping down
    on truncation: regular-hours bars ~26 days, extended-hours (premarket) ~7 days, VIX ~9 days."""
    from ..tastytrade_client import TastytradeClient
    from .datahub import history_batch
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    token = client.quote_token().get("data", {})
    now = datetime.now(timezone.utc)
    failures: dict[str, str] = {}

    async def fetch(series: dict[str, str], windows: list[int]) -> dict[str, list[dict]]:
        got_all: dict[str, list[dict]] = {}
        pending = dict(series)
        for days in windows:
            if not pending:
                break
            start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
            names = list(pending)
            for i in range(0, len(names), 15):
                chunk = {n: pending[n] for n in names[i:i + 15]}
                got, failed = await history_batch(dxlink_url=token["dxlink-url"], quote_token=token["token"],
                                                  series=chunk, start=start, timeout_seconds=240)
                got_all.update(got)
                for n in got:
                    pending.pop(n, None)
                for n, why in failed.items():
                    failures[f"{n}@{days}d"] = why
        for n in pending:
            failures[n] = "no complete snapshot at any window"
        return got_all
    rth_rows = await fetch({s: f"{s}{{=1m,a=s,tho=true}}" for s in symbols}, [26, 21, 15])
    ext_rows = await fetch({s: f"{s}{{=1m,tho=false}}" for s in broad}, [7, 5, 3])
    vix_rows = await fetch({"VIX": "VIX{=1m}"}, [9, 6, 4])
    return ({s: _bars(r) for s, r in rth_rows.items()}, {s: _bars(r) for s, r in ext_rows.items()},
            {"failures": failures, "vix": vix_rows.get("VIX", [])})


def _vix_lookup(rows: list[dict]) -> tuple[dict[date, float], list[tuple[datetime, float]]]:
    """(last VIX value per ET date, minute series) from VIX candles."""
    daily: dict[date, float] = {}
    series = []
    from .datahub import daily_session_date
    for r in sorted(rows, key=lambda x: x["timestamp"]):
        stamp = datetime.fromisoformat(r["timestamp"]).astimezone(timezone.utc)
        if stamp.time() == time(0, 0):        # a daily candle: its session is the UTC date (see datahub)
            daily[daily_session_date(r["timestamp"])] = float(r["close"])
            continue
        daily[stamp.astimezone(ET).date()] = float(r["close"])
        series.append((stamp, float(r["close"])))
    return daily, series


def _metrics(trades: list[dict]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0}
    r = [t["r"] for t in trades]
    wins, losses = [x for x in r if x > 0], [x for x in r if x <= 0]
    equity = peak = drawdown = 0.0
    for value in r:
        equity += value; peak = max(peak, equity); drawdown = max(drawdown, peak - equity)
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {"trades": len(r), "win_rate": round(len(wins) / len(r), 3), "avg_win_r": round(sum(wins) / len(wins), 3) if wins else None,
            "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else None, "expectancy_r": round(sum(r) / len(r), 3),
            "total_r": round(sum(r), 2), "max_drawdown_r": round(drawdown, 2),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
            "net_pnl_100sh": round(sum(t["net"] for t in trades), 2), "fees": round(FEE_PER_TRADE * len(r), 2),
            "slippage": round(SLIPPAGE_PER_SHARE * 200 * len(r), 2)}


def _breakdown(trades: list[dict], key: str) -> dict[str, Any]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        groups[str(t.get(key))].append(t)
    return {k: _metrics(v) for k, v in sorted(groups.items())}


VARIANTS = {
    # name: (entry rule, V2 management on)
    "original": ("original", False),
    "original_plus_v2_management": ("original", True),
    "v2_filters_fixed_exits": ("v2", False),
    "v2": ("v2", True),
    # Ablations: every V2 stage except the confirmation wait (entry on the core signal bar, like the original).
    "v2_no_confirmation_fixed_exits": ("v2_noconf", False),
    "v2_no_confirmation": ("v2_noconf", True),
}


def replay_days(*, config: V2Config, trade_symbols: list[str], rth: dict[str, dict[date, list[Bar]]],
                premarket: dict[str, dict[date, dict]], vix_daily: dict[date, float],
                vix_minutes: list[tuple[datetime, float]], daily_closes: dict[str, list[tuple[str, float]]],
                days: list[date]) -> dict[str, list[dict]]:
    strategy = config.base
    cfg = config.data
    rules = strategy.rules
    sector_map = SectorMap(config.sector_map)
    calendar_data = copy.deepcopy(config.calendar)
    calendar_data["events"] = HISTORICAL_FOMC + calendar_data["events"]
    from ..calendar import MarketCalendar
    market_calendar = MarketCalendar()

    def next_session(day: date) -> date | None:
        for k in range(1, 8):
            if market_calendar.session_bounds(day + timedelta(days=k)):
                return day + timedelta(days=k)
        return None
    events = EventEngine(calendar_data, cfg["events"], next_session=next_session)
    noconf_cfg = copy.deepcopy(cfg)
    noconf_cfg["confirmation"]["enabled"] = False
    context_symbols = list(config.context_symbols())
    results: dict[str, list[dict]] = {name: [] for name in VARIANTS}
    vix_index = 0
    for day in days:
        day_bars = {s: rth[s][day] for s in set(trade_symbols) | set(context_symbols) if s in rth and day in rth[s]}
        if any(s not in day_bars for s in trade_symbols) or "SPY" not in day_bars:
            continue
        session_open = datetime.combine(day, time(9, 30), ET).astimezone(timezone.utc)
        # correlation from daily closes strictly before this day
        before = {s: [(d, c) for d, c in rows if d < day.isoformat()] for s, rows in daily_closes.items()}
        model = CorrelationModel(cfg["correlation"], sector_map, before)
        ctx = MarketContext(tuple(cfg["context"]["broad_symbols"]), tuple(cfg["context"]["sector_etfs"]),
                            int(rules["trend_ema_fast"]), int(rules["trend_ema_slow"]), int(rules["atr_period"]),
                            int(cfg["regime"]["efficiency_window_bars"]), float(cfg["context"]["max_bar_age_seconds"]),
                            float(cfg["context"]["vix_max_age_seconds"]))
        for s in context_symbols:
            if s not in day_bars:
                ctx.mark_unavailable(s, "no replay history")
        for s, sessions in rth.items():
            prior_days = [d for d in sessions if d < day]
            if prior_days:
                last = sessions[max(prior_days)]
                ctx.prior[s] = DailyLevels(max(prior_days).isoformat(), last[0].open, max(b.high for b in last),
                                           min(b.low for b in last), last[-1].close)
        for s in cfg["context"]["broad_symbols"]:
            if s in premarket and day in premarket[s]:
                ctx.premarket[s] = premarket[s][day]
        prior_vix_days = sorted(d for d in vix_daily if d < day)
        prev_vix = vix_daily[prior_vix_days[-1]] if prior_vix_days else None
        growing: dict[str, list[Bar]] = {s: [] for s in day_bars}
        for s in growing:
            ctx.register(s, growing[s])
        index_of = {s: {b.timestamp: i for i, b in enumerate(bars)} for s, bars in day_bars.items()}
        minutes = sorted({b.timestamp for s in trade_symbols for b in day_bars[s]})
        trackers = {s: BreakoutTracker(cfg["confirmation"], int(rules["breakout_lookback_bars"])) for s in trade_symbols}
        books = {name: {"open": [], "closed": [], "entries": 0, "consecutive": 0} for name in VARIANTS}
        last_minute = minutes[-1]
        for minute in minutes:
            now = minute + timedelta(minutes=1)
            for s in day_bars:
                i = index_of[s].get(minute)
                if i is not None:
                    growing[s].append(day_bars[s][i])
            while vix_index + 1 < len(vix_minutes) and vix_minutes[vix_index + 1][0] <= minute:
                vix_index += 1
            if vix_minutes and vix_minutes[vix_index][0] <= minute and vix_minutes[vix_index][0].astimezone(ET).date() == day:
                ctx.vix = VixReading(vix_minutes[vix_index][1], prev_vix, now, "replay VIX 1m")
            elif prev_vix is not None:
                ctx.vix = VixReading(prev_vix, vix_daily.get(prior_vix_days[-2]) if len(prior_vix_days) > 1 else None, now,
                                     "replay: prior-session VIX close (no intraday VIX history)")
            snap = ctx.snapshot(now, trade_symbols)
            regime = classify(snap, cfg["regime"])
            core: dict[str, tuple] = {}
            tracked: dict[str, dict] = {}
            for s in trade_symbols:
                if index_of[s].get(minute) is None:
                    continue
                signal, metrics, _ = evaluate_underlying_diagnostics(growing[s], strategy, opening_range_start=session_open)
                core[s] = (signal, metrics)
                tracked[s] = trackers[s].update(growing[s][-1], signal, metrics)
            for name, (entry_rule, managed) in VARIANTS.items():
                book = books[name]
                _manage_exits(book, minute, now, growing, core, cfg, rules, managed, last_minute, regime)
                candidates = []
                for s in core:
                    if any(p["underlying"] == s for p in book["open"]) or minute == last_minute:
                        continue
                    signal, metrics = core[s]
                    if entry_rule == "original":
                        if signal is None:
                            continue
                        side, stop_d, target_d = signal.side, float(metrics["stop_distance"]), float(metrics["target_distance"])
                        setup_type = "ORIGINAL_SIGNAL"
                    else:
                        tracker = tracked[s]
                        if entry_rule == "v2_noconf":
                            if signal is None or not tracker.get("setup"):
                                continue
                        elif tracker["state"] == "NO_SETUP" or tracker["state"] in TERMINAL or not tracker["actionable"]:
                            continue
                        inp = pipeline.CandidateInputs(
                            symbol=s, now=now, bars=growing[s], core_metrics=metrics, tracker=tracker, snapshot=snap,
                            regime=regime, strategy=strategy, v2=noconf_cfg if entry_rule == "v2_noconf" else cfg,
                            sector_map=sector_map, events=events, metrics={},
                            correlation=model, risk_state=RiskState(10_000, 0, 0, 0, book["entries"], len(book["open"])),
                            positions=book["open"], pending=[], cash=10_000, equity=10_000,
                            profile_name="ORIGINAL_ACCOUNT_PAPER", fetch_chain=_no_options)
                        report = pipeline.evaluate_candidate(inp)
                        # Stages 1-7 all passed exactly when the pipeline went on to request the option chain.
                        reached_options = report.stages.get("option_pricing", {}).get("reason", "").startswith(
                            "option chain request failed")
                        if not reached_options:
                            continue
                        setup = tracker["setup"]
                        if entry_rule == "v2_noconf":   # the core signal's own side and distances on this bar
                            side, stop_d, target_d = signal.side, float(metrics["stop_distance"]), float(metrics["target_distance"])
                            setup_type = "CORE_SIGNAL_GATED"
                        else:
                            side, stop_d, target_d = setup["side"], float(setup["stop_distance"]), float(setup["target_distance"])
                            setup_type = tracker["state"]
                    candidates.append((float(metrics.get("trend_efficiency") or 0.0), s, side, stop_d, target_d, setup_type))
                candidates.sort(reverse=True)
                for eff, s, side, stop_d, target_d, setup_type in candidates:
                    if len(book["open"]) >= int(strategy.risk["max_open_positions"]) or book["entries"] >= int(strategy.risk["max_trades_per_day"]):
                        break
                    if book["consecutive"] >= int(strategy.risk["max_consecutive_losses"]):
                        break
                    price = growing[s][-1].close
                    direction = 1 if side == "CALL" else -1
                    if entry_rule in ("v2", "v2_noconf"):
                        # same-minute fills re-check the cluster and aggregate caps against what was just opened
                        exposure = sum(UNIT_RISK for p in model.related_positions(s, side, book["open"]))
                        if exposure + UNIT_RISK > UNIT_RISK + 1e-9 or (len(book["open"]) + 1) * UNIT_RISK > \
                                cfg["portfolio"]["aggregate_max_loss_fraction"]["ORIGINAL_ACCOUNT_PAPER"] * 10_000 + 1e-9:
                            continue
                    book["entries"] += 1
                    book["open"].append({"trade_id": f"{name}-{s}-{minute.isoformat()}", "underlying": s,
                                         "right": "C" if side == "CALL" else "P", "quantity": 1, "entry_price": 1.0,
                                         "entry_fees": 0.0, "modeled_risk_dollars": UNIT_RISK, "entry_underlying": price,
                                         "stop_underlying": price - direction * stop_d,
                                         "target_underlying": price + direction * target_d, "entry_time": now.isoformat(),
                                         "side": side, "setup_type": setup_type, "regime": regime.regime})
        for name in VARIANTS:
            results[name].extend(books[name]["closed"])
    return results


def _no_options():
    raise _NoOptionsInReplay("option chain not available in replay")


def _manage_exits(book, minute, now, growing, core, cfg, rules, managed, last_minute, regime) -> None:
    for p in list(book["open"]):
        s = p["underlying"]
        bars = growing[s]
        if not bars or bars[-1].timestamp != minute or datetime.fromisoformat(p["entry_time"]) > minute:
            continue
        close = bars[-1].close
        direction = 1 if p["side"] == "CALL" else -1
        why = None
        if minute == last_minute:
            why = "session_close"
        elif (close - p["stop_underlying"]) * direction <= 0:
            why = "stop"
        elif (close - p["target_underlying"]) * direction >= 0:
            why = "target"
        if managed and why not in ("session_close",):
            atr = (core.get(s, (None, {}))[1] or {}).get("atr")
            mcfg = copy.deepcopy(cfg["management"])
            result = management_mod.manage(p, underlying=close, atr=atr, option_bid=math.inf, now=now, cfg=mcfg,
                                           atr_multiple=float(rules["stop_underlying_atr"]), exit_fees_estimate=0.0,
                                           slippage_per_contract=0.0)
            state = p.get("v2_management", {})
            if result["exit_reason"]:
                why = result["exit_reason"]
            elif mcfg["breakeven"]["enabled"] and state.get("breakeven_armed") and (close - p["entry_underlying"]) * direction <= 0:
                why = "v2_breakeven_stop"
        if not why:
            continue
        gross = (close - p["entry_underlying"]) * direction * 100
        net = gross - SLIPPAGE_PER_SHARE * 200 - FEE_PER_TRADE
        risk = max(abs(p["entry_underlying"] - p["stop_underlying"]) * 100, 0.01)
        book["open"].remove(p)
        book["closed"].append({"symbol": s, "side": p["side"], "entry_time": p["entry_time"], "exit_time": now.isoformat(),
                               "entry": p["entry_underlying"], "exit": close, "exit_reason": why, "net": net, "r": net / risk,
                               "setup_type": p["setup_type"], "regime": p["regime"]})
        book["consecutive"] = book["consecutive"] + 1 if net < 0 else 0


def run_replay(root: Path, cfg, *, source: str, in_sample_end: str, out: Path) -> dict[str, Any]:
    config = load_v2_config(root)
    trade_symbols = list(config.base.universe)
    context_symbols = list(config.context_symbols())
    broad = list(config.data["context"]["broad_symbols"])
    symbols = list(dict.fromkeys(trade_symbols + context_symbols))
    notes = []
    if source == "massive_cache":
        raw = load_massive_cache(root, symbols)
        missing = [s for s in symbols if s not in raw]
        if missing:
            notes.append(f"no cached history for {missing}: their sector checks are INSUFFICIENT_DATA in the replay")
        extended = {s: raw[s] for s in broad if s in raw}
        vix_rows: list[dict] = []
        try:
            from ..tastytrade_client import TastytradeClient
            from .datahub import history_batch
            client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
            token = client.quote_token().get("data", {})
            got, _ = asyncio.run(history_batch(dxlink_url=token["dxlink-url"], quote_token=token["token"],
                                               series={"VIX": "VIX{=1d}"}, start=datetime(2026, 5, 1, tzinfo=timezone.utc)))
            vix_rows = got.get("VIX", [])
        except Exception as exc:
            notes.append(f"VIX daily history unavailable ({type(exc).__name__}); regime used SPY ATR only")
        vix_daily, _ = _vix_lookup(vix_rows)
        vix_minutes: list[tuple[datetime, float]] = []
        notes.append("VIX: prior-session close (tastytrade daily candles); intraday VIX not replayed in this mode")
    else:
        raw, ext, extra = asyncio.run(load_tastytrade(cfg, symbols, broad))
        extended = ext
        if extra["failures"]:
            notes.append(f"history failures: {extra['failures']}")
        vix_daily, vix_minutes = _vix_lookup(extra["vix"])
    rth, premarket = {}, {}
    for s, bars in raw.items():
        rth[s], pre = _split(bars)
        if s in extended:
            premarket[s] = _split(extended[s])[1]
    daily_closes = {s: [(d.isoformat(), b[-1].close) for d, b in sorted(days.items())] for s, days in rth.items()}
    today = datetime.now(timezone.utc).astimezone(ET).date()
    days = sorted(d for d in set().union(*[set(rth[s]) for s in trade_symbols if s in rth]) if d < today)  # completed sessions
    results = replay_days(config=config, trade_symbols=trade_symbols, rth=rth, premarket=premarket, vix_daily=vix_daily,
                          vix_minutes=vix_minutes, daily_closes=daily_closes, days=days)
    split = date.fromisoformat(in_sample_end)
    summary: dict[str, Any] = {"source": source, "days": len(days), "first_day": days[0].isoformat() if days else None,
                               "last_day": days[-1].isoformat() if days else None, "in_sample_end": in_sample_end,
                               "trade_symbols": trade_symbols, "notes": notes, "model": __doc__.split("\n\n")[1].strip(),
                               "variants": {}}
    for name, trades in results.items():
        ins = [t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() <= split]
        oos = [t for t in trades if datetime.fromisoformat(t["entry_time"]).astimezone(ET).date() > split]
        summary["variants"][name] = {"all": _metrics(trades), "in_sample": _metrics(ins), "out_of_sample": _metrics(oos),
                                     "by_regime": _breakdown(trades, "regime"), "by_setup_type": _breakdown(trades, "setup_type"),
                                     "by_exit": _breakdown(trades, "exit_reason")}
    out = out if out.is_absolute() else root / out
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / f"{source}-{stamp}-summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    (out / f"{source}-{stamp}-trades.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    return summary
