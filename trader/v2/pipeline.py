"""V2 candidate evaluation: one structured, deterministic report per candidate and bar.

Stages (each recorded with status PASS / REJECT / WAIT / INSUFFICIENT_DATA / NOT_APPLICABLE / NOT_EVALUATED, the
reason and the inputs that decided it):
   1 technical_signal       the core strategy signal qualified at the breakout bar, and on this bar the core's own
                            trend (EMA 9/21), ATR and volatility-spike conditions still hold; with daily_trend enabled
                            the price must also be on the trade's side of its N-session average daily close;
                            entry_filters / late_entry_cutoff reject excluded symbols and times;
                            failed_level_reentry rejects a breakout whose same-side breakout already failed recently;
                            time_to_target rejects an entry with too little session left to reach its target
   2 breakout_confirmation  confirmation.py state (WAIT until actionable)
   3 market_regime          regime.py; the trade's direction must be allowed in the current regime
   4 sector_confirmation    sectors.py
   5 volume_confirmation    the breakout bar's relative volume met the strategy minimum; price on the trade's side of
                            V2's session VWAP
   6 events                 events.py (earnings, FOMC window); a window that ends later in the session is a WAIT
   7 portfolio_correlation  V2 trades-per-day limit (trade_limits), remaining aggregate and cluster budgets (pre-check,
                            then the full check in 11)
   8 option_pricing         ORIGINAL contract selection (option_selector.evaluate_options) within V2's budget
   9 implied_volatility     options.evaluate_option (spread, size, quote age, IV rank, IV/HV, contract IV)
  10 liquidity              part of 8-9: original OI/volume/spread rules plus displayed size
  11 position_sizing        original risk engine (assess_entry, loss limits) + original Greek economic gate
                            (expected net R) + full portfolio check + sizing.py
  12 final                  APPROVED / WAIT_FOR_CONFIRMATION / REJECTED / INSUFFICIENT_DATA
Expensive stages (8+) run only when 1-7 pass, so a candidate that is waiting or rejected costs no option requests.
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..fees import FeeSchedule, economic_gate
from ..models import Bar, OptionQuote
from ..option_selector import evaluate_options
from ..risk_engine import RiskState, assess_entry
from ..strategy_loader import Strategy
from . import indicators, portfolio as portfolio_mod
from .config import stop_and_target
from .correlation import CorrelationModel
from .events import EventEngine
from .options import evaluate_option, within_friction
from .regime import RegimeResult
from .sectors import SectorMap, sector_confirmation
from .sizing import size_long_option
from .vetoes import active_veto

ET = ZoneInfo("America/New_York")
STAGES = ("technical_signal", "breakout_confirmation", "market_regime", "sector_confirmation", "volume_confirmation",
          "events", "portfolio_correlation", "option_pricing", "implied_volatility", "liquidity", "position_sizing", "final")


def contract_range(cfg: dict[str, Any], strategy: Strategy) -> tuple[int, int, bool]:
    """(min_dte, max_dte, nearest_expiry_first) for the contracts V2 buys: contract_policy when enabled, else
    strategy.txt's own range (today's behaviour)."""
    policy = cfg.get("contract_policy") or {}
    if policy.get("enabled"):
        return int(policy["min_dte"]), int(policy["max_dte"]), bool(policy["nearest_expiry_first"])
    return int(strategy.rules["min_dte"]), int(strategy.rules["max_dte"]), False


def select_contract(chain: list[OptionQuote], side: str, now: datetime, strategy: Strategy, cfg: dict[str, Any], *,
                    budget: float, costs: float):
    """The original contract rules (option_selector.evaluate_options), within contract_policy's expirations. With
    nearest_expiry_first, expirations are tried nearest first and the first that yields an eligible contract wins."""
    min_dte, max_dte, nearest = contract_range(cfg, strategy)
    if (min_dte, max_dte) != (int(strategy.rules["min_dte"]), int(strategy.rules["max_dte"])):
        strategy = dataclasses.replace(strategy, rules={**strategy.rules, "min_dte": min_dte, "max_dte": max_dte})
    if not nearest:
        return evaluate_options(chain, side, now, strategy, max_risk_dollars_per_contract=budget, costs_per_contract=costs)
    audit: list[dict] = []
    for expiration in sorted({q.expiration for q in chain}):
        selected, rows = evaluate_options([q for q in chain if q.expiration == expiration], side, now, strategy,
                                          max_risk_dollars_per_contract=budget, costs_per_contract=costs)
        audit += rows
        if selected is not None:
            return selected, audit
    return None, audit


def greek_projection(option: OptionQuote, *, price: float, target: float, stop: float, quantity: int,
                     risk_dollars: float, strategy: Strategy) -> dict[str, Any]:
    """The original strategy's paper economic gate (trader/engine.py), computed identically for V2's entry."""
    holding_days = int(strategy.rules["max_holding_minutes"]) / 1440
    slippage_each = float(strategy.execution["slippage_per_contract"])
    round_trip_fees = FeeSchedule(commission_per_contract=float(strategy.execution["commission_per_contract"]),
                                  exchange_fee_per_contract=float(strategy.execution["exchange_fee_per_contract"])).estimate(1, legs=2)

    def option_change(move: float) -> float:
        return option.delta * move + 0.5 * option.gamma * move * move + option.theta * holding_days

    expected_exit = max(0.0, option.ask + option_change(target - price) - option.spread)
    stop_exit = max(0.0, option.ask + option_change(stop - price) - option.spread)
    fees = round_trip_fees * quantity
    slippage = slippage_each * quantity * 2
    loss_at_stop = min((option.ask - stop_exit) * 100 * quantity + fees + slippage, risk_dollars)
    gate = economic_gate(entry=option.ask, exit_price=expected_exit, contracts=quantity, spread_cost=0.0,
                         slippage=slippage, fees=fees, risk_dollars=max(loss_at_stop, 0.01),
                         min_net_r=float(strategy.execution["min_expected_net_r"]))
    return {"expected_exit": expected_exit, "expected_stop_exit": stop_exit, "modeled_loss_at_stop_dollars": loss_at_stop,
            "expected_net_r": gate["expected_r"], "net_opportunity": gate["net_opportunity"],
            "gross_opportunity": gate["gross_opportunity"], "accepted": gate["accepted"]}


@dataclass
class CandidateInputs:
    symbol: str
    now: datetime
    bars: list[Bar]
    core_metrics: dict[str, Any]
    tracker: dict[str, Any]
    snapshot: dict[str, Any]
    regime: RegimeResult
    strategy: Strategy                     # the cycle strategy (original rules; profile sizing/loss limits applied)
    v2: dict[str, Any]                     # V2 config data
    sector_map: SectorMap
    events: EventEngine
    metrics: dict[str, Any] | None         # tastytrade market-metrics for the symbol (None = unavailable)
    correlation: CorrelationModel
    risk_state: RiskState
    positions: list[dict]
    pending: list[dict]
    cash: float
    equity: float
    profile_name: str
    fetch_chain: Callable[[], list[OptionQuote]]
    data_fresh: bool = True
    entries_blocked: str | None = None    # e.g. kill switch / safety gate reason
    # Wall clock for quote/metrics age (the original engine also ages quotes against real time, not the bar time).
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    core_signal_now: bool = True           # the core signal qualified on THIS bar (required when confirmation is off)
    daily_closes: list[float] | None = None  # completed prior sessions' closes, oldest first (daily_trend stage-1 check)
    session_close: datetime | None = None    # today's actual close (early closes); None = 16:00 ET (late_entry_cutoff)
    vetoes: dict[str, Any] | None = None     # the advisor's veto file (ai_veto); None = no advisor / replay


@dataclass
class CandidateReport:
    symbol: str
    side: str | None
    timestamp: str
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcome: str = "REJECTED"
    reasons: list[str] = field(default_factory=list)
    entry: dict[str, Any] | None = None

    def set(self, stage: str, status: str, reason: str = "", /, **inputs: Any) -> str:
        # Stage inputs may carry their own module-level status/reason (e.g. sector or event checks); the stage's
        # decision always wins and the module's own value is kept as detail_status / detail_reason.
        detail = {("detail_" + k if k in ("status", "reason") else k): v for k, v in inputs.items()}
        self.stages[stage] = {**detail, "status": status, "reason": reason}
        return status

    def to_dict(self) -> dict[str, Any]:
        ordered = {name: self.stages.get(name, {"status": "NOT_EVALUATED", "reason": "earlier stage decided"})
                   for name in STAGES}
        return {"symbol": self.symbol, "side": self.side, "timestamp": self.timestamp, "outcome": self.outcome,
                "reasons": self.reasons, "stages": ordered, "entry": self.entry}


def _finish(report: CandidateReport) -> CandidateReport:
    statuses = [s["status"] for name, s in report.stages.items() if name != "final"]
    if "REJECT" in statuses:
        report.outcome = "REJECTED"
    elif "INSUFFICIENT_DATA" in statuses:
        report.outcome = "INSUFFICIENT_DATA"
    elif "WAIT" in statuses:
        report.outcome = "WAIT_FOR_CONFIRMATION"
    else:
        report.outcome = "APPROVED"
    report.reasons = [f"{name}: {s['reason']}" for name, s in report.stages.items()
                      if s["status"] in ("REJECT", "WAIT", "INSUFFICIENT_DATA")]
    report.set("final", report.outcome, "; ".join(report.reasons) or "all stages passed")
    return report


def evaluate_candidate(inp: CandidateInputs) -> CandidateReport:
    setup = inp.tracker.get("setup") or {}
    side = setup.get("side")
    report = CandidateReport(inp.symbol, side, inp.now.isoformat())
    cfg, rules, strategy = inp.v2, inp.strategy.rules, inp.strategy
    metrics = inp.core_metrics
    price = inp.bars[-1].close

    # 1 technical signal --------------------------------------------------------------------------
    if inp.entries_blocked:
        report.set("technical_signal", "REJECT", f"entries_blocked:{inp.entries_blocked}")
        return _finish(report)
    if not inp.data_fresh:
        report.set("technical_signal", "INSUFFICIENT_DATA", "underlying data not fresh (bar or quote)")
        return _finish(report)
    filters = cfg.get("entry_filters") or {}
    if filters.get("enabled"):
        local = inp.now.astimezone(ET).time()
        if inp.symbol in filters["exclude_symbols"]:
            report.set("technical_signal", "REJECT", f"{inp.symbol} is excluded from V2 entries (entry_filters)")
            return _finish(report)
        if time.fromisoformat(filters["skip_from_et"]) <= local < time.fromisoformat(filters["skip_until_et"]):
            report.set("technical_signal", "REJECT", f"no new entries {filters['skip_from_et']}-{filters['skip_until_et']} ET "
                       "(entry_filters)", decision_time_et=local.isoformat(timespec="minutes"))
            return _finish(report)
    veto_cfg = cfg.get("ai_veto") or {}
    veto_hit = None
    if veto_cfg.get("enabled") and side:
        # 2.8.0 (owner, 2026-11-11): the advisor may block a symbol (company news, a pending ruling, a halt). It can
        # never add a trade or change its size. With enforce false the veto is only recorded on the candidate.
        veto_hit = active_veto(inp.vetoes, inp.symbol, side, inp.now, int(veto_cfg["max_age_minutes"]))
        if veto_hit and veto_cfg["enforce"]:
            report.set("technical_signal", "REJECT", f"advisor veto on {inp.symbol}"
                       f"{' ' + veto_hit['side'] if veto_hit['side'] else ''} until "
                       f"{datetime.fromisoformat(veto_hit['until_utc']).astimezone(ET).strftime('%H:%M')} ET: "
                       f"{veto_hit['reason'][:160]} (ai_veto)", ai_veto=veto_hit)
            return _finish(report)
    late = cfg.get("late_entry_cutoff") or {}
    if late.get("enabled"):
        close = inp.session_close or datetime.combine(inp.now.astimezone(ET).date(), time(16, 0), ET)
        cutoff = close - timedelta(minutes=int(late["no_entries_final_minutes"]))
        if inp.now >= cutoff:
            report.set("technical_signal", "REJECT", f"no new entries in the last {late['no_entries_final_minutes']} "
                       "minutes before the close (late_entry_cutoff)",
                       cutoff_et=cutoff.astimezone(ET).isoformat(timespec="minutes"))
            return _finish(report)
    reentry = cfg.get("failed_level_reentry") or {}
    if reentry.get("enabled") and side:
        # 2.7.0 (owner, 2026-11-10): after a breakout closes back inside the opening range, the same-side breakout of
        # the same range is not traded again for lookback_minutes (IWM 11/10: failed at 14:11 and 14:55 ET, bought at
        # 14:52 and 15:29). Only failures before this setup's breakout bar count.
        window_start = inp.now - timedelta(minutes=int(reentry["lookback_minutes"]))
        trigger = setup.get("trigger_time")
        trigger_at = datetime.fromisoformat(trigger) if trigger else inp.now
        recent = [f for f in (inp.tracker.get("failures") or []) if f.get("side") == side
                  and window_start <= datetime.fromisoformat(f["at"]) < trigger_at]
        if recent:
            last = recent[-1]
            report.set("technical_signal", "REJECT", f"the same {side} breakout failed at "
                       f"{datetime.fromisoformat(last['at']).astimezone(ET).strftime('%H:%M')} ET, within "
                       f"{reentry['lookback_minutes']} minutes (failed_level_reentry)",
                       recent_failures=recent, lookback_minutes=int(reentry["lookback_minutes"]))
            return _finish(report)
    reach = cfg.get("time_to_target") or {}
    if reach.get("enabled"):
        # 2.7.0 (owner, 2026-11-10): the target must be within reach before the close. Reach = reach_multiple x
        # one-minute ATR x sqrt(minutes left), a random-walk estimate of how far price can travel in the time left.
        close = inp.session_close or datetime.combine(inp.now.astimezone(ET).date(), time(16, 0), ET)
        minutes_left = max(0.0, (close - inp.now).total_seconds() / 60)
        atr_now, target_d = metrics.get("atr"), setup.get("target_distance")
        if atr_now is None or target_d is None:
            report.set("technical_signal", "INSUFFICIENT_DATA", "ATR or target distance unknown (time_to_target)")
            return _finish(report)
        reachable = float(reach["reach_multiple"]) * float(atr_now) * math.sqrt(minutes_left)
        if float(target_d) > reachable:
            report.set("technical_signal", "REJECT", f"target {float(target_d):.2f} away but only about {reachable:.2f} "
                       f"is reachable in the {minutes_left:.0f} minutes left (time_to_target)",
                       minutes_left=round(minutes_left, 1), target_distance=float(target_d), atr=float(atr_now),
                       reachable=round(reachable, 4), reach_multiple=float(reach["reach_multiple"]))
            return _finish(report)
    width = cfg.get("min_opening_range") or {}
    if width.get("enabled"):
        if setup.get("range_high") is None or setup.get("range_low") is None or not price > 0:
            report.set("technical_signal", "INSUFFICIENT_DATA", "opening range unknown (min_opening_range)")
            return _finish(report)
        equity = inp.sector_map.group(inp.symbol) == "us_equity"
        need = float(width["equity_fraction" if equity else "etf_fraction"])
        height = (float(setup["range_high"]) - float(setup["range_low"])) / price
        if height < need:
            report.set("technical_signal", "REJECT", f"opening range too narrow ({height:.4f} < {need:.4f} of the price; "
                       "min_opening_range)", range_fraction=round(height, 5), required_fraction=need)
            return _finish(report)
    fast, slow, atr = metrics.get("ema_fast"), metrics.get("ema_slow"), metrics.get("atr")
    trend_ok = fast is not None and slow is not None and ((fast > slow) if side == "CALL" else (fast < slow))
    atr_ok = atr is not None and price > 0 and atr / price <= float(rules["max_atr_fraction"])
    spike = metrics.get("atr_expansion")
    spike_ok = spike is not None and spike <= float(rules["max_atr_expansion"])
    tech = {"breakout_bar_utc": setup.get("trigger_time"), "ema_fast": fast, "ema_slow": slow, "atr": atr,
            "atr_fraction": atr / price if atr and price else None, "atr_expansion": spike}
    if veto_hit:
        tech["ai_veto_not_enforced"] = veto_hit
    daily = cfg.get("daily_trend") or {}
    daily_ok = True
    if daily.get("enabled"):
        sessions = int(daily["sma_sessions"])
        closes = list(inp.daily_closes or [])[-sessions:]
        sma = sum(closes) / len(closes) if len(closes) == sessions else None
        tech.update(daily_sma=sma, daily_sma_sessions=sessions)
        if sma is None:
            report.set("technical_signal", "INSUFFICIENT_DATA", f"fewer than {sessions} daily closes for the daily trend", **tech)
            return _finish(report)
        daily_ok = (price > sma) if side == "CALL" else (price < sma)
    if not (trend_ok and atr_ok and spike_ok and daily_ok):
        failed = [n for n, ok in (("ema_trend", trend_ok), ("atr_filter", atr_ok), ("volatility_spike", spike_ok),
                                  ("daily_trend", daily_ok)) if not ok]
        report.set("technical_signal", "REJECT", "core conditions no longer hold on this bar: " + ",".join(failed), **tech)
    else:
        report.set("technical_signal", "PASS", "core signal qualified at the breakout bar and still holds", **tech)

    # 2 breakout confirmation ------------------------------------------------------------------------
    state = inp.tracker["state"]
    if not cfg["confirmation"]["enabled"]:
        # Without the confirmation wait, V2 enters exactly where the original would: on a bar whose core signal qualifies.
        if inp.core_signal_now:
            report.set("breakout_confirmation", "NOT_APPLICABLE", "confirmation stage disabled; core signal on this bar", state=state)
        else:
            report.set("breakout_confirmation", "REJECT", "confirmation stage disabled and no core signal on this bar", state=state)
    elif inp.tracker["actionable"]:
        report.set("breakout_confirmation", "PASS", state, setup=setup)
    elif state in ("AWAITING_CONFIRMATION", "PULLBACK_HOLDING"):
        report.set("breakout_confirmation", "WAIT", state, setup=setup)
    else:
        report.set("breakout_confirmation", "REJECT", state, setup=setup)

    # 3 regime ------------------------------------------------------------------------------------
    regime = inp.regime
    if not cfg["regime"]["enabled"]:
        report.set("market_regime", "NOT_APPLICABLE", "regime stage disabled", regime=regime.regime)
    elif regime.regime == "UNCLEAR":
        report.set("market_regime", "INSUFFICIENT_DATA", "regime UNCLEAR: " + "; ".join(regime.notes), **regime.to_dict())
    elif side in regime.allowed:
        report.set("market_regime", "PASS", f"{regime.regime} allows {side}", **regime.to_dict())
    else:
        report.set("market_regime", "REJECT", f"{regime.regime} does not allow {side}", **regime.to_dict())

    # 4 sector ------------------------------------------------------------------------------------
    if not cfg["sector"]["enabled"]:
        report.set("sector_confirmation", "NOT_APPLICABLE", "sector stage disabled")
    else:
        symbol_state = {"change_from_open": indicators.change(price, inp.bars[0].open) if inp.bars else None}
        sector = sector_confirmation(inp.symbol, side, inp.sector_map, inp.snapshot.get("sectors", {}), cfg["sector"], symbol_state)
        status = {"PASS": "PASS", "REJECT": "REJECT", "NOT_APPLICABLE": "NOT_APPLICABLE"}.get(sector["status"], "INSUFFICIENT_DATA")
        report.set("sector_confirmation", status, sector.get("reason", ""), **sector)

    # 5 volume / VWAP -------------------------------------------------------------------------------
    rv = setup.get("trigger_relative_volume")
    vwap = indicators.session_vwap(inp.bars)
    volume_ok = rv is not None and rv >= float(rules["relative_volume_min"])
    vwap_ok = (not cfg["vwap_alignment"]["enabled"]) or (vwap is not None and ((price > vwap) if side == "CALL" else (price < vwap)))
    details = {"breakout_bar_relative_volume": rv, "relative_volume_min": rules["relative_volume_min"],
               "this_bar_relative_volume": metrics.get("relative_volume"), "session_vwap": vwap, "price": price}
    if vwap is None and cfg["vwap_alignment"]["enabled"]:
        report.set("volume_confirmation", "INSUFFICIENT_DATA", "session VWAP unavailable", **details)
    elif not volume_ok:
        report.set("volume_confirmation", "REJECT", "breakout bar relative volume below the strategy minimum", **details)
    elif not vwap_ok:
        report.set("volume_confirmation", "REJECT", f"price on the wrong side of session VWAP for a {side}", **details)
    else:
        report.set("volume_confirmation", "PASS", "breakout volume confirmed; VWAP aligned", **details)

    # 6 events ------------------------------------------------------------------------------------
    events = inp.events.check(inp.symbol, inp.metrics, inp.now, single_stock=inp.sector_map.is_single_stock(inp.symbol))
    status = {"PASS": "PASS", "REJECT": "REJECT", "DELAY": "WAIT"}[events["outcome"]]
    report.set("events", status, "; ".join(events.get("reasons", [])) or "no blocking event", **events)

    # 7 portfolio pre-check -------------------------------------------------------------------------
    max_fraction = min(float(strategy.risk["max_risk_per_trade_fraction"]), float(strategy.risk["hard_max_risk_per_trade_fraction"]))
    per_trade = max(0.0, inp.equity * max_fraction)
    budgets = portfolio_mod.budgets_for(equity=inp.equity, per_trade_budget=per_trade,
                                        aggregate_fraction=cfg["portfolio"]["aggregate_max_loss_fraction"][inp.profile_name],
                                        positions=inp.positions, pending=inp.pending)
    cluster_open, related = portfolio_mod.cluster_exposure(inp.correlation, inp.symbol, side, inp.positions) \
        if cfg["correlation"]["enabled"] else (0.0, [])
    cluster_remaining = max(0.0, budgets.cluster_cap - cluster_open)
    contract_budget = min(budgets.per_trade, budgets.aggregate_remaining, cluster_remaining)
    pre = {"per_trade_budget": round(budgets.per_trade, 2), "aggregate_cap": round(budgets.aggregate_cap, 2),
           "aggregate_open_max_loss": round(budgets.aggregate_open, 2), "cluster_cap": round(budgets.cluster_cap, 2),
           "cluster_open_max_loss": round(cluster_open, 2), "correlated_positions": related,
           "contract_budget": round(contract_budget, 2)}
    limits = cfg.get("trade_limits") or {}
    if limits.get("enabled") and inp.risk_state.trades_today >= int(limits["max_entries_per_session"]):
        report.set("portfolio_correlation", "REJECT", f"V2 limit of {limits['max_entries_per_session']} trades per day reached",
                   trades_today=inp.risk_state.trades_today, **pre)
    elif not cfg["portfolio"]["enabled"]:
        contract_budget = budgets.per_trade
        report.set("portfolio_correlation", "NOT_APPLICABLE", "portfolio stage disabled", **pre)
    elif contract_budget <= 0:
        report.set("portfolio_correlation", "REJECT", "no aggregate or correlated-cluster budget left", **pre)
    else:
        report.set("portfolio_correlation", "PASS", "budget available", **pre)

    if any(s["status"] != "PASS" and s["status"] != "NOT_APPLICABLE" for s in report.stages.values()):
        return _finish(report)

    # 8-10 option selection, pricing, volatility, liquidity ------------------------------------------
    # 2.9.0: stop_policy may widen the strategy's stop and target (unchanged when absent, as in 2.8.0).
    stop_distance, target_distance = stop_and_target(cfg, price, float(setup["stop_distance"]), float(setup["target_distance"]))
    direction = 1 if side == "CALL" else -1
    stop, target = price - direction * stop_distance, price + direction * target_distance
    try:
        chain = inp.fetch_chain()
    except Exception as exc:  # network/API failure: never guess
        report.set("option_pricing", "INSUFFICIENT_DATA", f"option chain request failed: {type(exc).__name__}")
        return _finish(report)
    slippage_each = float(strategy.execution["slippage_per_contract"])
    round_trip_fees = FeeSchedule(commission_per_contract=float(strategy.execution["commission_per_contract"]),
                                  exchange_fee_per_contract=float(strategy.execution["exchange_fee_per_contract"])).estimate(1, legs=2)
    costs = 2 * slippage_each + round_trip_fees
    friction_cap = strategy.rules.get("max_friction_fraction")   # account-profile rule; strategy.txt has no such key
    friction_skipped = 0
    if friction_cap is not None:
        eligible_chain = within_friction(chain, float(friction_cap), costs)
        friction_skipped = len(chain) - len(eligible_chain)
        chain = eligible_chain
    selected, audit = select_contract(chain, side, inp.now, strategy, cfg, budget=contract_budget, costs=costs)
    if selected is None:
        rejections = sorted({r for row in audit for r in row["rejection_reasons"]} |
                            ({"friction_above_maximum"} if friction_skipped else set())) or ["no_option_contracts_returned"]
        report.set("option_pricing", "REJECT", "original contract rules found no eligible contract within budget: "
                   + ",".join(rejections), contracts_evaluated=len(audit), contract_budget=round(contract_budget, 2))
        return _finish(report)
    option = selected.quote
    report.set("option_pricing", "PASS", f"selected {option.symbol} by the original contract rules",
               contract=option.symbol, expiration=option.expiration.isoformat(), strike=option.strike,
               dte=(option.expiration.date() - inp.now.date()).days, contracts_evaluated=len(audit),
               cost_per_contract=round(option.ask * 100 + costs, 2))
    quality = evaluate_option(option, inp.metrics, inp.clock(), cfg["option_quality"]) if cfg["option_quality"]["enabled"] \
        else {"ok": True, "reasons": [], "flags": [], "pricing": {}, "volatility": {}, "status": "disabled"}
    vol_reasons = [r for r in quality["reasons"] if r.startswith(("iv_", "contract_iv", "underlying_volatility", "volatility_metrics"))]
    liq_reasons = [r for r in quality["reasons"] if r not in vol_reasons]
    report.set("implied_volatility", "REJECT" if vol_reasons else "PASS", "; ".join(vol_reasons) or "volatility acceptable",
               volatility=quality["volatility"], flags=quality["flags"])
    report.set("liquidity", "REJECT" if liq_reasons else "PASS", "; ".join(liq_reasons) or
               "original OI/volume/spread rules passed; displayed size and quote age acceptable", pricing=quality["pricing"])
    if not quality["ok"]:
        return _finish(report)

    # 11 risk engine, economic gate, full portfolio check, sizing -------------------------------------
    max_contracts = min(int(rules["max_contracts"]), int(cfg["sizing"]["max_contracts"]))
    risk = assess_entry(state=inp.risk_state, contract_price=option.ask, strategy_risk=strategy.risk,
                        max_contracts=max_contracts, data_fresh=inp.data_fresh, account_certain=True, reconciled=True,
                        protection_verified=True, estimated_entry_slippage_per_contract=slippage_each,
                        estimated_exit_slippage_per_contract=slippage_each,
                        estimated_round_trip_fees_per_contract=round_trip_fees, underlying=inp.symbol)
    sizing_inputs = {"risk_engine": risk.reason, "risk_engine_quantity": risk.quantity,
                     "modeled_risk_dollars": round(risk.risk_dollars, 2), "premium_exposure_dollars": risk.premium_exposure_dollars,
                     "planned_risk_dollars": risk.planned_risk_dollars}
    if not risk.allowed:
        report.set("position_sizing", "REJECT", f"original risk engine: {risk.reason}", **sizing_inputs)
        return _finish(report)
    if option.delta is None or option.gamma is None or option.theta is None:
        report.set("position_sizing", "INSUFFICIENT_DATA", "option greeks missing for the economic gate", **sizing_inputs)
        return _finish(report)
    gate = greek_projection(option, price=price, target=target, stop=stop, quantity=risk.quantity,
                            risk_dollars=risk.risk_dollars, strategy=strategy)
    sizing_inputs["economic_gate"] = gate
    cost_cfg = cfg.get("cost_gate") or {}
    cost_r, cost_enforced = None, False
    if cost_cfg.get("enabled"):
        # 2.8.0 (owner, 2026-11-11): what this contract costs to trade, as a fraction of the trade's own planned risk
        # (trader/cost_model.py). No symbol is banned; a trade is skipped only when its own cost is too large for its
        # own stop. Where enforce is false for the profile the figure is logged and nothing is blocked.
        cost_dollars = (option.ask - option.bid) * 100 + round_trip_fees + 2 * slippage_each
        r_dollars = abs(option.delta) * stop_distance * 100
        cost_r = cost_dollars / r_dollars if r_dollars > 0 else None
        limit = float(cost_cfg["max_cost_r"][inp.profile_name])
        cost_enforced = bool(cost_cfg["enforce"][inp.profile_name])
        over = cost_r is None or cost_r > limit
        sizing_inputs["cost_gate"] = {"cost_r": round(cost_r, 4) if cost_r is not None else None, "max_cost_r": limit,
                                      "round_trip_cost_dollars": round(cost_dollars, 2),
                                      "one_r_dollars": round(r_dollars, 2), "over_limit": over, "enforced": cost_enforced}
        if over and cost_enforced:
            report.set("position_sizing", "REJECT", f"round-trip option cost {cost_r if cost_r is not None else float('nan'):.2f}R "
                       f"above the {limit}R limit (cost_gate)", **sizing_inputs)
            return _finish(report)
    if not gate["accepted"] and not (cost_enforced and cost_cfg.get("replaces_expected_net_r_gate")):
        report.set("position_sizing", "REJECT", f"expected net R {gate['expected_net_r']:.2f} below the strategy minimum "
                   f"{strategy.execution['min_expected_net_r']}", **sizing_inputs)
        return _finish(report)
    favorites = cfg.get("favorites") or {}
    if favorites.get("enabled") and inp.symbol not in favorites["symbols"]:
        # Favorites are looked at first: same-minute candidates are ranked by expected net R, so another symbol only
        # takes a slot from a favorite by scoring higher, and on its own it must clear this higher minimum.
        need = float(favorites["others_min_expected_net_r"])
        if gate["expected_net_r"] < need:
            report.set("position_sizing", "REJECT", f"{inp.symbol} is not a favorite: expected net R "
                       f"{gate['expected_net_r']:.2f} below the non-favorite minimum {need}", **sizing_inputs)
            return _finish(report)
    max_loss_each = risk.risk_dollars / risk.quantity
    cash_each = option.ask * 100 + slippage_each + round_trip_fees / 2
    sizing = size_long_option(max_loss_per_contract=max_loss_each, per_trade_budget=budgets.per_trade,
                              aggregate_remaining=budgets.aggregate_remaining, cluster_remaining=cluster_remaining,
                              cash=inp.cash, cash_per_contract=cash_each, bid_size=option.bid_size,
                              ask_size=option.ask_size, open_interest=option.open_interest, cfg=cfg["sizing"],
                              hard_max_contracts=max_contracts)
    quantity = min(sizing["quantity"], risk.quantity)
    sizing_inputs["sizing"] = sizing
    if quantity < 1:
        report.set("position_sizing", "REJECT", f"no affordable quantity (limited by {sizing['limiting_factor']})", **sizing_inputs)
        return _finish(report)
    candidate_loss = max_loss_each * quantity
    check = portfolio_mod.evaluate(model=inp.correlation, symbol=inp.symbol, side=side, candidate_max_loss=candidate_loss,
                                   entry_cash=cash_each * quantity, cash=inp.cash, positions=inp.positions, budgets=budgets) \
        if cfg["portfolio"]["enabled"] else portfolio_mod.PortfolioCheck(True, [], {})
    sizing_inputs["portfolio"] = check.details
    if not check.ok:
        report.set("position_sizing", "REJECT", "; ".join(check.reasons), **sizing_inputs)
        return _finish(report)
    report.set("position_sizing", "PASS", f"{quantity} contract(s); expected net R {gate['expected_net_r']:.2f}", **sizing_inputs)
    report.entry = {"option": option, "quantity": quantity, "stop_underlying": stop, "target_underlying": target,
                    "entry_underlying": price, "modeled_risk_dollars": candidate_loss,
                    "planned_risk_dollars": risk.planned_risk_dollars,
                    "premium_exposure_dollars": option.ask * 100 * quantity, "expected_net_r": gate["expected_net_r"],
                    "cost_r": round(cost_r, 4) if cost_r is not None else None,
                    "trend_efficiency": metrics.get("trend_efficiency"), "setup_type": state, "regime": inp.regime.regime,
                    "quality": {"regime": inp.regime.regime, "confirmation": state, "expected_net_r": gate["expected_net_r"],
                                "trend_efficiency": metrics.get("trend_efficiency"),
                                "breakout_relative_volume": rv, "affects_size": False}}
    return _finish(report)


def report_record(report: CandidateReport) -> dict[str, Any]:
    """JSON-safe decision record (the OptionQuote object is flattened)."""
    data = report.to_dict()
    if report.entry:
        entry = dict(report.entry)
        option = entry.pop("option")
        entry["option"] = {"symbol": option.symbol, "bid": option.bid, "ask": option.ask, "delta": option.delta,
                           "iv": option.iv, "expiration": option.expiration.isoformat(), "strike": option.strike}
        data["entry"] = entry
    return data
