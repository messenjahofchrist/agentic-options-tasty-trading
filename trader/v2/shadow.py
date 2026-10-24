"""Non-executing benchmark: the ORIGINAL strategy, run bar by bar on the same live data as V2, on its own simulated
ledger (data_dir/v2_shadow_original). It uses the original decision cycle, risk engine, contract selection, same-
minute ranking and exit rules, and the same PAPER fill and fee model. It never submits an order of any kind and
never touches the V2 ledger: it only reads quotes and writes its own files.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..broker_fees import TastytradeOptionFees
from ..engine import decision_cycle
from ..logging_utils import append_jsonl
from ..models import Bar, OptionQuote
from ..paper_execution import PaperExecutor, realized_option_pnl
from ..portfolio import load_or_initialize, save_account
from ..position_manager import exit_reason
from ..risk_engine import RiskState
from ..strategy_loader import Strategy


class ShadowOriginal:
    def __init__(self, *, root: Path, data_dir: Path, strategy: Strategy, starting_cash: float, profile=None,
                 arbiter_factory: Callable | None = None, helpers: dict[str, Callable]):
        self.root = root
        self.dir = data_dir / "v2_shadow_original"
        self.account_path = self.dir / "paper_account.json"
        self.account = load_or_initialize(self.account_path, starting_cash)
        self.strategy = strategy
        self.profile = profile
        self.h = helpers            # _daily_realized, _weekly_realized, _consecutive_losses, _trades_today,
                                    # _unrealized_pnl, _position_from_record, _option_marks, _fresh_quote
        self.executor = PaperExecutor(self.account, buy_price=strategy.execution["paper_buy_price"],
                                      sell_price=strategy.execution["paper_sell_price"],
                                      slippage_per_contract=float(strategy.execution["slippage_per_contract"]),
                                      fees=TastytradeOptionFees())
        self.decision_log = self.dir / "decisions.jsonl"
        self.events_log = self.dir / "shadow_events.jsonl"
        self.arbiter = arbiter_factory(self.available_slots) if arbiter_factory else None

    def equity(self) -> float:
        a = self.account
        return max(0.0, a.starting_capital + a.deposits - a.withdrawals + a.realized_pnl + a.unrealized_pnl)

    def available_slots(self, at: datetime | None = None) -> int:
        moment = at or datetime.now(timezone.utc)
        return min(int(self.strategy.risk["max_open_positions"]) - len(self.account.positions),
                   int(self.strategy.risk["max_trades_per_day"]) - self.h["trades_today"](self.account, moment))

    def _book_exit(self, record: dict, quote: OptionQuote, why: str, when: datetime) -> float:
        position = self.h["position_from_record"](record)
        fill = self.executor.fill(quote, "SELL", position.quantity, why, when)
        realized = realized_option_pnl(entry_fill_price=position.entry_price, exit_fill_price=fill.fill_price,
                                       quantity=position.quantity, entry_fees=float(record["entry_fees"]), exit_fees=fill.fees)
        self.account.realized_pnl += realized
        self.account.ledger.append({"type": "paper_close", "trade_id": position.trade_id, "realized_pnl": realized,
                                    "actual_realized_pnl": realized, "modeled_risk_dollars": record.get("modeled_risk_dollars"),
                                    "actual_r_multiple": realized / max(float(record.get("modeled_risk_dollars", 0)), 1e-9),
                                    "underlying": record["underlying"], "reason": why, "timestamp": when.isoformat(),
                                    "strategy_version": record["strategy_version"], "strategy_hash": record["strategy_hash"],
                                    "shadow_benchmark": True, "fees": fill.fees})
        self.account.positions.remove(record)
        self.account.unrealized_pnl = self.h["unrealized_pnl"](self.account, self.executor)
        append_jsonl(self.events_log, {"event": "shadow_exit", "underlying": record["underlying"], "option": record["symbol"],
                                       "reason": why, "realized_pnl": round(realized, 2), "at": when.isoformat()})
        return realized

    async def on_bar(self, *, client, symbol: str, bars: list[Bar], bar: Bar, bar_now: datetime, session_close: datetime,
                     calendar, quote_fresh: bool, data_valid: bool, chain: Callable[[], Any],
                     opening_range_start: datetime | None = None) -> None:
        """Manage the shadow's own positions in `symbol`, then let the original decision cycle decide an entry."""
        account = self.account
        flatten = bool(self.strategy.execution["flatten_at_session_close"])
        for record in [p for p in account.positions if p["underlying"] == symbol]:
            try:
                quote = (await asyncio.to_thread(self.h["option_marks"], client, [record]))[0]
            except Exception:
                continue
            if not self.h["fresh_quote"](quote, datetime.now(timezone.utc)) or record not in account.positions:
                continue
            record["last_bid"] = quote.bid
            account.unrealized_pnl = self.h["unrealized_pnl"](account, self.executor)
            position = self.h["position_from_record"](record)
            daily = self.h["daily_realized"](account, bar_now) + account.unrealized_pnl
            why = ("end_of_session_close" if flatten and bar_now >= session_close - timedelta(minutes=1) else
                   exit_reason(position, underlying=bar.close, option_bid=quote.bid, now=bar_now,
                               daily_loss_hit=daily <= -self.equity() * float(self.strategy.risk["max_daily_loss_fraction"]),
                               enable_time_exit=bool(self.strategy.execution["enable_time_exit"])))
            if why:
                self._book_exit(record, quote, why, bar_now)
            else:
                record["mfe"], record["mae"] = position.mfe, position.mae
        equity = self.equity()
        strategy = self.strategy if self.profile is None else self.profile.cycle_strategy(self.strategy, equity, account)
        entry_window = calendar.entry_window_ok(bar_now, strategy.entry_window_et["start"], strategy.entry_window_et["end"])
        # The chain is fetched only when the original signal qualifies, and shared with V2 for the same bar.
        from ..strategy_engine import evaluate_underlying
        wants_chain = entry_window and data_valid and quote_fresh and evaluate_underlying(bars, strategy, opening_range_start=opening_range_start) is not None
        chain_quotes = await chain() if wants_chain else []
        risk_state = RiskState(equity=equity, daily_pnl=self.h["daily_realized"](account, bar_now) + account.unrealized_pnl,
                               weekly_pnl=self.h["weekly_realized"](account, bar_now) + account.unrealized_pnl,
                               consecutive_losses=self.h["consecutive_losses"](account),
                               trades_today=self.h["trades_today"](account, bar_now), open_positions=len(account.positions),
                               open_underlyings=tuple(str(p["underlying"]) for p in account.positions))
        records = await asyncio.to_thread(
            decision_cycle, strategy=replace(strategy, universe=(symbol,)), strategy_path=self.root / "strategy.txt",
            manifest_path=self.root / "active_strategy.json", bars_by_symbol={symbol: list(bars)},
            option_chain_by_symbol={symbol: chain_quotes or []}, risk_state=risk_state, now=bar_now,
            data_fresh=data_valid and quote_fresh, account_certain=True, reconciled=True, protection_verified=True,
            market_open=calendar.is_open(bar_now), within_entry_window=entry_window, decision_log=self.decision_log,
            kill_switch_path=self.root / "KILL_SWITCH", paper_execution=True,
            opening_range_start=opening_range_start)
        for decision in records:
            if decision.decision != "PAPER_ENTRY":
                continue
            score = (float(decision.diagnostics.get("expected_net_r") or 0.0),
                     float((decision.diagnostics.get("signal_metrics") or {}).get("trend_efficiency") or 0.0))
            if self.arbiter is not None and not await self.arbiter.approve(bar.timestamp, symbol, score):
                continue
            if self.available_slots(bar_now) < 1 or any(p["underlying"] == symbol for p in account.positions):
                continue
            if self.profile is not None:
                ok, _ = self.profile.entry_allowed(account, equity, decision.diagnostics["modeled_risk_dollars"])
                if not ok:
                    continue
            selected = next((q for q in chain_quotes or [] if q.symbol == decision.diagnostics.get("option_candidate")), None)
            if selected is None:
                continue
            quantity = int(decision.diagnostics["quantity"])
            try:
                fill = self.executor.fill(selected, "BUY", quantity, "strategy_entry", bar_now)
            except ValueError:
                continue
            account.positions.append({
                "trade_id": fill.trade_id, "symbol": selected.symbol, "underlying": symbol, "right": selected.right,
                "quantity": quantity, "entry_price": fill.fill_price, "entry_fees": fill.fees, "entry_underlying": bar.close,
                "stop_underlying": decision.diagnostics["underlying_stop_trigger"],
                "target_underlying": decision.diagnostics["underlying_target_trigger"], "entry_time": bar_now.isoformat(),
                "max_holding_minutes": int(strategy.rules["max_holding_minutes"]),
                "initial_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                "modeled_risk_dollars": decision.diagnostics["modeled_risk_dollars"],
                "expected_net_r": decision.diagnostics.get("expected_net_r"), "strategy_version": strategy.version,
                "strategy_hash": strategy.sha256, "mfe": 0.0, "mae": 0.0, "last_bid": selected.bid, "shadow_benchmark": True})
            account.unrealized_pnl = self.h["unrealized_pnl"](account, self.executor)
            append_jsonl(self.events_log, {"event": "shadow_entry", "underlying": symbol, "option": selected.symbol,
                                           "fill": fill.fill_price, "expected_net_r": score[0], "at": bar_now.isoformat()})
        save_account(self.account_path, account)

    def flatten(self, client, when: datetime) -> None:
        """End of session: close what is still open at fresh bids, exactly when the original engine would
        (flatten_at_session_close); otherwise the positions carry, as the original's would."""
        if not self.account.positions or not bool(self.strategy.execution["flatten_at_session_close"]):
            save_account(self.account_path, self.account)
            return
        try:
            marks = {q.symbol: q for q in self.h["option_marks"](client, list(self.account.positions))}
        except Exception:
            return
        for record in list(self.account.positions):
            quote = marks.get(record["symbol"])
            if quote and self.h["fresh_quote"](quote, when):
                self._book_exit(record, quote, "end_of_session_close", when)
        save_account(self.account_path, self.account)

    def summary(self) -> dict[str, Any]:
        closes = [x for x in self.account.ledger if x.get("type") == "paper_close"]
        return {"cash": round(self.account.cash, 2), "realized_pnl": round(self.account.realized_pnl, 2),
                "closed_trades": len(closes), "open_positions": len(self.account.positions)}
