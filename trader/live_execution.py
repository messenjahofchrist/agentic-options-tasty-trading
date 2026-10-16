"""LIVE order execution for the existing strategy loop.

The strategy decides; this module only executes. `paper_runner._run` calls `LiveTrader` at the
exact points where PAPER calls `PaperExecutor.fill`, with the same decision, contract, quantity and
exit reason. Nothing here selects contracts, sizes positions or decides exits.

Order safety rules:
- Every order intent is journaled to disk before any network call, with a unique external-identifier.
- Every order is dry-run first; errors or warnings block it.
- A submission is never assumed filled: fills come only from the broker's order record.
- An uncertain submission (timeout, network drop, 5xx) is resolved by looking the external-identifier
  up at the broker. It is never blindly resubmitted.
- A cancellation is never assumed: the order is polled until the broker reports a terminal status.
- Any order whose final state cannot be confirmed blocks new entries until it is resolved.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .calendar import MarketCalendar
from .config import AppConfig, Mode
from .logging_utils import append_jsonl, atomic_replace
from .models import OptionQuote
from .notifications import record_critical_alert
from .paper_execution import realized_option_pnl
from .portfolio import PaperAccount, load_or_initialize, save_account
from .reconciliation import reconcile_positions
from .strategy_loader import Strategy
from .tastytrade_client import BrokerError, TastytradeClient, UncertainOrderSubmission

TERMINAL_STATUSES = {"Filled", "Cancelled", "Expired", "Rejected", "Removed", "Partially Removed"}
# Options levels that permit buying calls and puts to open. Anything else blocks LIVE with the exact level.
LONG_OPTION_LEVELS = {"No Restrictions", "Defined Risk Spreads", "Covered And Cash Secured"}
LIVE_AUTHORIZATION_FILE = "live_authorization.json"
LIVE_AUTHORIZATION_STATEMENT = "I AUTHORIZE LIVE EXECUTION OF THIS EXACT STRATEGY HASH"


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
        return self.client.submit_order(self.config.account_number, body, dry_run_result=preflight)


@dataclass(frozen=True)
class ExecutionSettings:
    """Execution-layer timing only. These are not strategy parameters and never change a decision."""
    entry_fill_timeout_seconds: float = 20.0
    exit_fill_timeout_seconds: float = 10.0
    exit_max_attempts: int = 6
    cancel_confirm_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 1.0
    uncertain_lookup_attempts: int = 3
    uncertain_lookup_delay_seconds: float = 3.0
    broker_state_max_age_seconds: float = 90.0
    broker_refresh_seconds: float = 30.0


@dataclass
class OrderOutcome:
    external_identifier: str
    order_id: str | None
    status: str
    requested_quantity: int
    filled_quantity: int = 0
    average_fill_price: float | None = None
    fees: float = 0.0
    confirmed: bool = True  # False when the broker's final state could not be established
    detail: str = ""


def mask_account(account: str) -> str:
    return ("*" * max(0, len(account) - 4)) + account[-4:] if account else ""


def account_fingerprint(account: str) -> str:
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _signed_quantity(item: dict) -> int:
    quantity = int(_float(item.get("quantity")))
    return -quantity if str(item.get("quantity-direction", "Long")).lower() == "short" else quantity


def parse_fills(order: dict) -> tuple[int, float | None]:
    """Total filled quantity and average fill price from the broker's order record."""
    quantity, notional = 0, 0.0
    for leg in order.get("legs") or []:
        for fill in leg.get("fills") or []:
            filled = int(_float(fill.get("quantity")))
            quantity += filled
            notional += filled * _float(fill.get("fill-price"))
    if quantity == 0:
        # Some responses carry only remaining-quantity on the leg.
        for leg in order.get("legs") or []:
            size, remaining = _float(leg.get("quantity")), leg.get("remaining-quantity")
            if remaining is not None and size > _float(remaining):
                quantity += int(size - _float(remaining))
        return quantity, None if quantity == 0 else (_float(order.get("price")) or None)
    return quantity, notional / quantity


def verify_live_authorization(root: Path, strategy: Strategy, account_number: str) -> dict:
    """LIVE runs only an exact, separately authorized strategy hash on the configured account.

    The authorization lives outside strategy.txt/active_strategy.json, so the strategy and its
    PAPER-designated manifest stay byte-for-byte unchanged.
    """
    path = root / LIVE_AUTHORIZATION_FILE
    if not path.is_file():
        raise RuntimeError(f"LIVE blocked: {LIVE_AUTHORIZATION_FILE} not found. The owner must run "
                           "`python -m trader.cli authorize-live` for this exact strategy hash.")
    record = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if record.get("strategy_sha256") != strategy.sha256:
        problems.append("strategy hash differs from the authorized hash")
    if record.get("strategy_id") != strategy.strategy_id or record.get("strategy_version") != strategy.version:
        problems.append("strategy id/version differs from the authorization")
    if record.get("account_fingerprint") != account_fingerprint(account_number):
        problems.append("TASTYTRADE_ACCOUNT_NUMBER differs from the authorized account")
    if record.get("statement") != LIVE_AUTHORIZATION_STATEMENT:
        problems.append("authorization statement missing")
    if problems:
        raise RuntimeError("LIVE blocked: " + "; ".join(problems) + ". Re-run authorize-live if this change is intended.")
    return record


class LiveTrader:
    """Executes the strategy's entries/exits on the configured tastytrade account and keeps a LIVE ledger.

    The LIVE ledger reuses the PaperAccount record shape so the existing risk-state helpers compute
    daily/weekly P/L, loss streaks and trade counts from actual broker fills exactly as PAPER does from
    simulated ones. It is stored separately (data/live/) and never touches the paper ledger.
    """

    def __init__(self, *, cfg: AppConfig, client: TastytradeClient, strategy: Strategy, data_dir: Path,
                 kill_switch_path: Path, settings: ExecutionSettings | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep, monotonic: Callable[[], float] = time.monotonic):
        if cfg.mode is not Mode.LIVE:
            raise RuntimeError("LiveTrader requires TRADING_MODE=LIVE")
        cfg.validate_startup()
        self.cfg, self.client, self.strategy = cfg, client, strategy
        self.account_number = cfg.account_number
        self.data_dir = data_dir
        self.kill_switch_path = kill_switch_path
        self.settings = settings or ExecutionSettings()
        self._sleep, self._monotonic = sleep, monotonic
        self.account_path = data_dir / "live_account.json"
        self.journal_path = data_dir / "live_orders.jsonl"
        self.intents_path = data_dir / "live_open_intents.json"
        self.account: PaperAccount | None = None
        self.pending_entries: set[str] = set()
        self.entry_block_reasons: dict[str, str] = {}
        self.net_liquidating_value: float | None = None
        self.derivative_buying_power: float | None = None
        self.settled_cash: float | None = None
        self.margin_or_cash: str | None = None
        self.calendar = MarketCalendar()
        self.broker_state_at: float | None = None
        self.unmanaged_positions: list[dict] = []
        self.startup_report: dict = {}
        self._entry_lock = asyncio.Lock()
        self._last_refresh = 0.0

    # ----- persistence -------------------------------------------------------------------------
    def _journal(self, event: str, **fields: Any) -> None:
        append_jsonl(self.journal_path, {"mode": "LIVE", "event": event,
                                         "account": mask_account(self.account_number), **fields})

    def _intents(self) -> dict[str, dict]:
        try:
            return json.loads(self.intents_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save_intents(self, intents: dict[str, dict]) -> None:
        self.intents_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.intents_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(intents, indent=2, default=str) + "\n", encoding="utf-8")
        atomic_replace(temporary, self.intents_path)

    def _set_intent(self, external_id: str, **fields: Any) -> None:
        intents = self._intents()
        intents[external_id] = {**intents.get(external_id, {}), **fields,
                                "updated_at_utc": datetime.now(timezone.utc).isoformat()}
        self._save_intents(intents)

    def _clear_intent(self, external_id: str) -> None:
        intents = self._intents()
        if intents.pop(external_id, None) is not None:
            self._save_intents(intents)

    def save(self) -> None:
        if self.account is not None:
            save_account(self.account_path, self.account)

    # ----- entry gating ------------------------------------------------------------------------
    def block_entries(self, key: str, reason: str) -> None:
        if self.entry_block_reasons.get(key) != reason:
            self.entry_block_reasons[key] = reason
            self._journal("entries_blocked", key=key, reason=reason)
            record_critical_alert("live_entries_blocked", mode="LIVE", details={"key": key, "reason": reason},
                                  data_dir=self.data_dir)

    def unblock_entries(self, key: str) -> None:
        if self.entry_block_reasons.pop(key, None) is not None:
            self._journal("entries_unblocked", key=key)

    @property
    def broker_state_fresh(self) -> bool:
        return (self.broker_state_at is not None and
                self._monotonic() - self.broker_state_at <= self.settings.broker_state_max_age_seconds)

    @property
    def reconciled(self) -> bool:
        return self.broker_state_fresh and not any(key.startswith(("reconcile", "order:", "broker"))
                                                   for key in self.entry_block_reasons)

    @property
    def account_certain(self) -> bool:
        return self.broker_state_fresh and "account" not in self.entry_block_reasons

    @property
    def protection_verified(self) -> bool:
        # Exits are the strategy's own underlying-trigger rules, monitored by this process. Protection is
        # "verified" while broker state is fresh and no position has an unresolved exit failure.
        return self.broker_state_fresh and not any(key.startswith("exit:") for key in self.entry_block_reasons)

    def equity(self) -> float:
        """Account equity for the strategy's risk formulas: the broker's net liquidating value."""
        if self.net_liquidating_value is None:
            raise RuntimeError("LIVE equity unknown: broker balances have not been retrieved")
        return max(0.0, self.net_liquidating_value)

    # ----- cash-account settlement rule (owner-authorized account rule; not a strategy parameter) ---------
    @property
    def settlement_rule_applies(self) -> bool:
        """Enforced on every account the broker does not report as a margin account (unknown counts as cash)."""
        return str(self.margin_or_cash or "").lower() != "margin"

    def settlement_date(self, traded_at: datetime) -> date:
        """Equity options settle T+1: the next trading session after the ET trade date."""
        day = traded_at.astimezone(ZoneInfo("America/New_York")).date() + timedelta(days=1)
        while not self.calendar.session_bounds(day):
            day += timedelta(days=1)
        return day

    def unsettled_sale_proceeds(self, now: datetime) -> float:
        """Net proceeds of this bot's LIVE sells that have not reached their settlement date."""
        today = now.astimezone(ZoneInfo("America/New_York")).date()
        total = 0.0
        for row in (self.account.ledger if self.account is not None else []):
            if row.get("type") != "fill" or row.get("mode") != "LIVE" or row.get("side") != "SELL":
                continue
            if self.settlement_date(datetime.fromisoformat(row["timestamp"])) > today:
                total += float(row["fill_price"]) * 100 * int(row["quantity"]) - float(row.get("fees") or 0)
        return max(0.0, total)

    def settled_funds_available(self, now: datetime) -> float | None:
        """Money a purchase may use without touching unsettled proceeds: the broker's derivative buying power
        less this bot's unsettled sale proceeds, capped by the broker's settled cash balance."""
        if self.derivative_buying_power is None:
            return None
        if not self.settlement_rule_applies:
            return self.derivative_buying_power
        if self.settled_cash is None:
            return None
        return max(0.0, min(self.derivative_buying_power - self.unsettled_sale_proceeds(now), self.settled_cash))

    # ----- startup -----------------------------------------------------------------------------
    def startup(self) -> dict:
        """Verify the exact configured account, load the LIVE ledger, resolve old intents, reconcile."""
        account = self.account_number
        report: dict[str, Any] = {"account": mask_account(account), "checked_at_utc": datetime.now(timezone.utc).isoformat()}
        items = self.client.accounts().get("data", {}).get("items", [])
        match = next((item for item in items if (item.get("account") or {}).get("account-number") == account), None)
        if match is None:
            raise RuntimeError(f"LIVE blocked: configured account {mask_account(account)} is not accessible with these "
                               f"credentials ({len(items)} other account(s) visible; none is used as a fallback)")
        info = match.get("account") or {}
        authority = str(match.get("authority-level", ""))
        report.update({"authority_level": authority, "margin_or_cash": info.get("margin-or-cash"),
                       "account_type": info.get("account-type-name"), "is_closed": info.get("is-closed")})
        if authority.lower() not in {"owner", "trade-only", "trade"}:
            raise RuntimeError(f"LIVE blocked: credentials have '{authority}' authority on the account, not trading authority")
        if info.get("is-closed"):
            raise RuntimeError("LIVE blocked: the configured account is closed")
        self.margin_or_cash = info.get("margin-or-cash")
        status = self.client.trading_status(account).get("data", {})
        flags = {name: bool(status.get(name)) for name in ("is-closed", "is-frozen", "is-closing-only",
                                                           "is-risk-reducing-only", "is-in-margin-call",
                                                           "is-in-day-trade-equity-maintenance-call")}
        report.update({"options_level": status.get("options-level"), "is_pattern_day_trader": status.get("is-pattern-day-trader"),
                       "trading_status_flags": flags})
        if any(flags.values()):
            raise RuntimeError("LIVE blocked: account trading status " + ", ".join(k for k, v in flags.items() if v))
        if status.get("options-level") not in LONG_OPTION_LEVELS:
            raise RuntimeError(f"LIVE blocked: options level '{status.get('options-level')}' does not permit buying options to open")
        self.account = load_or_initialize(self.account_path, 0.0)
        self._resolve_open_intents()
        self.refresh_broker_state(force=True)
        if not self.reconciled:
            report["entry_block_reasons"] = dict(self.entry_block_reasons)
        report.update({"net_liquidating_value": self.net_liquidating_value,
                       "derivative_buying_power": self.derivative_buying_power,
                       "settlement_rule_enforced": self.settlement_rule_applies,
                       "settled_funds_available": self.settled_funds_available(datetime.now(timezone.utc)),
                       "ledger_positions": len(self.account.positions),
                       "unmanaged_positions": self.unmanaged_positions,
                       "entry_block_reasons": dict(self.entry_block_reasons)})
        self.startup_report = report
        self._journal("startup_verified", **{k: v for k, v in report.items() if k != "account"})
        return report

    def _resolve_open_intents(self) -> None:
        """Restart recovery: settle every order intent that had not reached a confirmed terminal state."""
        for external_id, intent in list(self._intents().items()):
            order = self._lookup_order(external_id, intent.get("order_id"))
            if order is None:
                if intent.get("stage") in {"pre_submit", "submitting", "uncertain_not_found"}:
                    # Never reached the broker, or repeatedly absent since an uncertain submission.
                    self._journal("intent_never_submitted", external_identifier=external_id, stage=intent.get("stage"))
                    self._clear_intent(external_id)
                else:
                    self.block_entries(f"order:{external_id}",
                                       "an order submitted before the restart was not found at the broker; review")
                continue
            if order.get("status") not in TERMINAL_STATUSES:
                order = self._cancel_and_confirm_sync(order)
            self._apply_recovered_order(intent, order)
            if order.get("status") in TERMINAL_STATUSES:
                self._clear_intent(external_id)
            else:
                self.block_entries(f"order:{external_id}", f"order {order.get('id')} still {order.get('status')} after restart")

    def _apply_recovered_order(self, intent: dict, order: dict) -> None:
        """Book fills that happened while the process was down so the ledger matches the broker."""
        assert self.account is not None
        filled, price = parse_fills(order)
        booked = int(intent.get("booked_quantity", 0))
        if filled <= booked or price is None:
            return
        quantity = filled - booked
        self._journal("recovered_fill", external_identifier=intent.get("external_identifier"), order_id=order.get("id"),
                      quantity=quantity, price=price, purpose=intent.get("purpose"))
        if intent.get("purpose") == "entry" and intent.get("position_template"):
            self._book_entry(intent["position_template"], quantity, price, float(intent.get("fees_per_contract", 0)) * quantity,
                             order.get("id"), intent.get("external_identifier"))
        elif intent.get("purpose") == "exit":
            record = next((p for p in self.account.positions if p["trade_id"] == intent.get("trade_id")), None)
            if record is not None:
                self._book_exit(record, quantity, price, float(intent.get("fees_per_contract", 0)) * quantity,
                                intent.get("reason", "recovered_exit"), datetime.now(timezone.utc),
                                order.get("id"), intent.get("external_identifier"))
        self.save()

    # ----- broker state & reconciliation -------------------------------------------------------
    def refresh_broker_state(self, *, force: bool = False) -> None:
        if not force and self._monotonic() - self._last_refresh < self.settings.broker_refresh_seconds:
            return
        self._last_refresh = self._monotonic()
        try:
            balances = self.client.balances(self.account_number).get("data", {})
            positions = self.client.positions(self.account_number).get("data", {}).get("items", [])
            orders = self.client.live_orders(self.account_number).get("data", {}).get("items", [])
        except Exception as exc:
            self.block_entries("broker_state", f"broker state refresh failed: {type(exc).__name__}")
            return
        self.unblock_entries("broker_state")
        self.net_liquidating_value = _float(balances.get("net-liquidating-value"), None)  # type: ignore[arg-type]
        self.derivative_buying_power = _float(balances.get("derivative-buying-power"), None)  # type: ignore[arg-type]
        if self.net_liquidating_value is None or self.derivative_buying_power is None:
            self.block_entries("account", "balances response lacked net-liquidating-value/derivative-buying-power")
            return
        self.settled_cash = _float(balances.get("cash-settle-balance"), None)  # type: ignore[arg-type]
        if self.settlement_rule_applies and self.settled_cash is None:
            self.block_entries("account", "cash account: balances response lacked cash-settle-balance")
            return
        self.unblock_entries("account")
        self.broker_state_at = self._monotonic()
        self._recheck_uncertain_intents()
        self.reconcile(positions, orders)

    def _recheck_uncertain_intents(self) -> None:
        """An uncertain submission still absent a minute later was not accepted; release its entry block."""
        for external_id, intent in list(self._intents().items()):
            if intent.get("stage") != "uncertain_not_found":
                continue
            updated = datetime.fromisoformat(intent["updated_at_utc"])
            if (datetime.now(timezone.utc) - updated).total_seconds() < 60:
                continue
            try:
                order = self._lookup_order(external_id)
            except BrokerError:
                continue
            if order is None:
                self._journal("uncertain_submission_confirmed_absent", external_identifier=external_id)
                self._clear_intent(external_id)
                self.unblock_entries(f"order:{external_id}")
            else:
                self._set_intent(external_id, stage="submitted", order_id=order.get("id"))
                if order.get("status") in TERMINAL_STATUSES:
                    self._apply_recovered_order(self._intents()[external_id], order)
                    self._clear_intent(external_id)
                    self.unblock_entries(f"order:{external_id}")

    def _in_flight_symbols(self) -> set[str]:
        return {leg.get("symbol") for intent in self._intents().values()
                for leg in (intent.get("order") or {}).get("legs", [])}

    def reconcile(self, broker_positions: list[dict], broker_orders: list[dict]) -> None:
        assert self.account is not None
        universe = set(self.strategy.universe)
        managed = [p for p in broker_positions if p.get("instrument-type") == "Equity Option"
                   and str(p.get("underlying-symbol")) in universe]
        self.unmanaged_positions = [{"symbol": p.get("symbol"), "instrument_type": p.get("instrument-type"),
                                     "quantity": _signed_quantity(p)}
                                    for p in broker_positions if p not in managed]
        in_flight = self._in_flight_symbols()  # an order being worked right now is reconciled once it settles
        local = [{"symbol": p["symbol"], "quantity": int(p["quantity"]), "instrument_type": "Equity Option"}
                 for p in self.account.positions if p["symbol"] not in in_flight]
        broker = [{"symbol": p["symbol"], "quantity": _signed_quantity(p), "instrument_type": "Equity Option"}
                  for p in managed if p["symbol"] not in in_flight]
        result = reconcile_positions(local, broker)
        if result.ok:
            self.unblock_entries("reconcile_positions")
        else:
            self.block_entries("reconcile_positions", "broker/ledger position mismatch: " + "; ".join(result.discrepancies))
        known = {intent.get("order_id") for intent in self._intents().values()} | \
                {intent.get("external_identifier") for intent in self._intents().values()}
        foreign = [o for o in broker_orders if o.get("status") not in TERMINAL_STATUSES
                   and str(o.get("underlying-symbol")) in universe
                   and o.get("id") not in known and o.get("external-identifier") not in known]
        if foreign:
            self.block_entries("reconcile_orders", f"{len(foreign)} working order(s) on strategy underlyings not placed by this session")
        else:
            self.unblock_entries("reconcile_orders")

    def broker_quantity(self, option_symbol: str) -> int | None:
        try:
            items = self.client.positions(self.account_number).get("data", {}).get("items", [])
        except Exception:
            return None
        return sum(_signed_quantity(p) for p in items if p.get("symbol") == option_symbol)

    async def refresh(self) -> None:
        await asyncio.to_thread(self.refresh_broker_state)

    # ----- independent pre-trade safety --------------------------------------------------------
    def entry_safety_failures(self, *, underlying: str, quote: OptionQuote, quantity: int, decision_option: str | None,
                              readiness: dict, now: datetime, quote_fresh: Callable[[OptionQuote, datetime], bool]) -> list[str]:
        rules = self.strategy.rules
        failures = []
        if self.kill_switch_path.exists():
            failures.append("kill_switch_active")
        if self.entry_block_reasons:
            failures.append("entries_blocked:" + ",".join(sorted(self.entry_block_reasons)))
        if not self.broker_state_fresh:
            failures.append("broker_state_stale")
        if readiness.get("final_readiness") not in {"READY"}:
            failures.append(f"symbol_not_ready:{readiness.get('final_readiness')}")
        if readiness.get("historical_to_live_transition_status") not in {"VERIFIED", "NOT_REQUIRED_LIVE_CAPTURE"}:
            failures.append("historical_to_live_transition_unverified")
        if quote.symbol != decision_option or quote.underlying != underlying:
            failures.append("contract_does_not_match_strategy_decision")
        if not quote_fresh(quote, now):
            failures.append("option_quote_stale_or_invalid")
        if quote.ask <= 0 or quote.bid < 0 or quote.ask < quote.bid:
            failures.append("option_quote_invalid")
        elif (quote.spread > float(rules["max_spread_dollars"]) + 1e-9 or
              quote.spread_fraction > float(rules["max_spread_fraction"]) + 1e-9):
            failures.append("option_spread_exceeds_strategy_limit")
        if not 1 <= quantity <= int(rules["max_contracts"]):
            failures.append("quantity_outside_strategy_contract_limit")
        if self.derivative_buying_power is None or quote.ask * 100 * quantity > self.derivative_buying_power:
            failures.append("insufficient_derivative_buying_power")
        if self.settlement_rule_applies:
            execution = self.strategy.execution
            cost = quantity * (quote.ask * 100 + float(execution["commission_per_contract"])
                               + float(execution["exchange_fee_per_contract"]))
            settled = self.settled_funds_available(now)
            if settled is None or cost > settled + 1e-9:
                failures.append("insufficient_settled_funds_cash_account")
        if self.account is not None and any(p["underlying"] == underlying for p in self.account.positions):
            failures.append("position_already_open_in_underlying")
        return failures

    # ----- order lifecycle ---------------------------------------------------------------------
    def _order_body(self, *, option_symbol: str, action: str, quantity: int, limit: float, external_id: str) -> dict:
        return {"time-in-force": "Day", "order-type": "Limit", "price": f"{limit:.2f}",
                "price-effect": "Debit" if action.startswith("Buy") else "Credit",
                "external-identifier": external_id,
                "legs": [{"instrument-type": "Equity Option", "symbol": option_symbol,
                          "quantity": quantity, "action": action}]}

    def _lookup_order(self, external_id: str, order_id: Any = None) -> dict | None:
        if order_id:
            try:
                return self.client.order(self.account_number, order_id).get("data")
            except BrokerError:
                pass
        return self.client.find_order_by_external_id(self.account_number, external_id)

    def _cancel_and_confirm_sync(self, order: dict) -> dict:
        try:
            self.client.cancel_order(self.account_number, order["id"])
        except BrokerError as exc:
            self._journal("cancel_request_error", order_id=order.get("id"), error=str(exc)[:300])
        deadline = self._monotonic() + self.settings.cancel_confirm_timeout_seconds
        while self._monotonic() < deadline:
            order = self.client.order(self.account_number, order["id"]).get("data") or order
            if order.get("status") in TERMINAL_STATUSES:
                return order
            time.sleep(self.settings.poll_interval_seconds)
        return order

    async def _poll_until(self, order_id: Any, timeout: float) -> dict:
        deadline = self._monotonic() + timeout
        order: dict = {}
        while True:
            order = (await asyncio.to_thread(self.client.order, self.account_number, order_id)).get("data") or {}
            if order.get("status") in TERMINAL_STATUSES or self._monotonic() >= deadline:
                return order
            await self._sleep(self.settings.poll_interval_seconds)

    async def execute_order(self, *, option_symbol: str, action: str, quantity: int, limit: float,
                            fill_timeout: float, intent: dict) -> OrderOutcome:
        external_id = f"optiobot-{uuid.uuid4()}"
        body = self._order_body(option_symbol=option_symbol, action=action, quantity=quantity, limit=limit,
                                external_id=external_id)
        self._set_intent(external_id, **intent, external_identifier=external_id, stage="pre_submit",
                         order=body, booked_quantity=0)
        self._journal("order_intent", external_identifier=external_id, order=body, purpose=intent.get("purpose"))
        try:
            preflight = await asyncio.to_thread(self.client.dry_run_order, self.account_number, body)
        except BrokerError as exc:
            self._clear_intent(external_id)
            self._journal("dry_run_rejected", external_identifier=external_id, error=str(exc)[:500])
            return OrderOutcome(external_id, None, "DRY_RUN_REJECTED", quantity, detail=str(exc)[:500])
        data = preflight.get("data") or {}
        if data.get("errors") or data.get("warnings") or not (data.get("order") or data.get("complex-order")):
            self._clear_intent(external_id)
            self._journal("dry_run_blocked", external_identifier=external_id,
                          errors=data.get("errors"), warnings=data.get("warnings"))
            return OrderOutcome(external_id, None, "DRY_RUN_BLOCKED", quantity,
                                detail=json.dumps({"errors": data.get("errors"), "warnings": data.get("warnings")})[:500])
        fee_total = _float(((data.get("fee-calculation") or {}).get("total-fees")), quantity * 0.70)
        self._set_intent(external_id, stage="submitting", fees_per_contract=fee_total / max(quantity, 1))
        order: dict | None = None
        try:
            response = await asyncio.to_thread(self.client.submit_order, self.account_number, body, dry_run_result=preflight)
            order = (response.get("data") or {}).get("order") or response.get("data")
        except UncertainOrderSubmission as exc:
            self._journal("submission_uncertain", external_identifier=external_id, error=str(exc)[:300])
            for attempt in range(self.settings.uncertain_lookup_attempts):
                await self._sleep(self.settings.uncertain_lookup_delay_seconds)
                try:
                    order = await asyncio.to_thread(self.client.find_order_by_external_id, self.account_number, external_id)
                except BrokerError:
                    order = None
                if order is not None:
                    break
            if order is None:
                # Not found after repeated lookups: the broker did not accept it. Do not resubmit; the
                # strategy's next bar decides again. Keep the intent and block entries until a later check
                # confirms the identifier never appears.
                self._set_intent(external_id, stage="uncertain_not_found")
                self.block_entries(f"order:{external_id}", "order submission outcome uncertain and not found at broker")
                return OrderOutcome(external_id, None, "UNCERTAIN_NOT_FOUND", quantity, confirmed=False)
        except BrokerError as exc:
            self._clear_intent(external_id)
            self._journal("submission_rejected", external_identifier=external_id, error=str(exc)[:500])
            return OrderOutcome(external_id, None, "REJECTED", quantity, detail=str(exc)[:500])
        order_id = order.get("id") if order else None
        if not order_id:
            self.block_entries(f"order:{external_id}", "broker accepted the request but returned no order id")
            return OrderOutcome(external_id, None, "UNKNOWN", quantity, confirmed=False)
        self._set_intent(external_id, stage="submitted", order_id=order_id)
        self._journal("order_submitted", external_identifier=external_id, order_id=order_id, status=order.get("status"))
        order = await self._poll_until(order_id, fill_timeout)
        if order.get("status") not in TERMINAL_STATUSES:
            self._journal("order_cancel_requested", order_id=order_id, status=order.get("status"))
            order = await asyncio.to_thread(self._cancel_and_confirm_sync, order)
        filled, price = parse_fills(order)
        status = str(order.get("status"))
        confirmed = status in TERMINAL_STATUSES
        if not confirmed:
            self.block_entries(f"order:{external_id}", f"order {order_id} status {status} could not be confirmed terminal")
        self._journal("order_final", external_identifier=external_id, order_id=order_id, status=status,
                      filled_quantity=filled, average_fill_price=price, confirmed=confirmed)
        return OrderOutcome(external_id, str(order_id), status, quantity, filled, price,
                            fee_total * filled / max(quantity, 1), confirmed)

    # ----- ledger booking ----------------------------------------------------------------------
    def _book_entry(self, template: dict, quantity: int, price: float, fees: float, order_id: Any, external_id: Any) -> dict:
        assert self.account is not None
        now = datetime.now(timezone.utc)
        record = {**template, "quantity": quantity, "entry_price": price, "entry_fees": fees,
                  "broker_order_id": order_id, "external_identifier": external_id, "mode": "LIVE"}
        self.account.ledger.append({"type": "fill", "mode": "LIVE", "trade_id": record["trade_id"], "side": "BUY",
                                    "symbol": record["symbol"], "quantity": quantity, "fill_price": price, "fees": fees,
                                    "slippage": 0.0, "reason": "strategy_entry", "broker_order_id": order_id,
                                    "external_identifier": external_id, "timestamp": now.isoformat(),
                                    "fill_model": "broker_reported_fill", "strategy_id": record.get("strategy_id"),
                                    "strategy_version": record.get("strategy_version"),
                                    "strategy_hash": record.get("strategy_hash"), "underlying": record.get("underlying"),
                                    "planned_risk_dollars": record.get("planned_risk_dollars"),
                                    "modeled_risk_dollars": record.get("modeled_risk_dollars"),
                                    "premium_exposure_dollars": record.get("premium_exposure_dollars")})
        self.account.positions.append(record)
        self.account.fees_paid += fees
        return record

    def _book_exit(self, record: dict, quantity: int, price: float, fees: float, reason: str, when: datetime,
                   order_id: Any, external_id: Any) -> float:
        assert self.account is not None
        held = int(record["quantity"])
        entry_fees_share = float(record.get("entry_fees", 0)) * quantity / held
        realized = realized_option_pnl(entry_fill_price=float(record["entry_price"]), exit_fill_price=price,
                                       quantity=quantity, entry_fees=entry_fees_share, exit_fees=fees)
        self.account.ledger.append({"type": "fill", "mode": "LIVE", "trade_id": str(uuid.uuid4()), "side": "SELL",
                                    "symbol": record["symbol"], "quantity": quantity, "fill_price": price, "fees": fees,
                                    "slippage": 0.0, "reason": reason, "broker_order_id": order_id,
                                    "external_identifier": external_id, "timestamp": when.isoformat(),
                                    "fill_model": "broker_reported_fill", "strategy_id": record.get("strategy_id"),
                                    "strategy_version": record["strategy_version"], "strategy_hash": record["strategy_hash"]})
        self.account.ledger.append({"type": "live_close", "mode": "LIVE", "trade_id": record["trade_id"],
                                    "quantity": quantity, "realized_pnl": realized, "actual_realized_pnl": realized,
                                    "planned_risk_dollars": record.get("planned_risk_dollars"),
                                    "modeled_risk_dollars": record.get("modeled_risk_dollars"),
                                    "premium_exposure_dollars": record.get("premium_exposure_dollars"),
                                    "actual_r_multiple": realized / max(float(record.get("modeled_risk_dollars") or 0), 1e-9),
                                    "strategy_id": record.get("strategy_id"), "strategy_version": record["strategy_version"],
                                    "strategy_hash": record["strategy_hash"], "underlying": record["underlying"],
                                    "reason": reason, "broker_order_id": order_id, "timestamp": when.isoformat()})
        self.account.realized_pnl += realized
        self.account.fees_paid += fees
        if quantity >= held:
            self.account.positions.remove(record)
        else:
            record["quantity"] = held - quantity
            record["entry_fees"] = float(record.get("entry_fees", 0)) - entry_fees_share
        return realized

    def _settle_intents_for(self, trade_id: str) -> bool:
        """Resolve leftover exit intents for one position. True when none remain unresolved."""
        for external_id, intent in list(self._intents().items()):
            if intent.get("trade_id") != trade_id:
                continue
            try:
                order = self._lookup_order(external_id, intent.get("order_id"))
            except BrokerError:
                return False
            if order is None and intent.get("stage") in {"pre_submit", "submitting"}:
                self._clear_intent(external_id)
                continue
            if order is None or order.get("status") not in TERMINAL_STATUSES:
                return False
            self._apply_recovered_order(intent, order)
            self._clear_intent(external_id)
            self.unblock_entries(f"order:{external_id}")
        return True

    # ----- the two calls the strategy loop makes -----------------------------------------------
    async def open_position(self, *, template: dict, quote: OptionQuote, quantity: int, decision_option: str | None,
                            readiness: dict, now: datetime,
                            quote_fresh: Callable[[OptionQuote, datetime], bool]) -> dict | None:
        """Execute the strategy's entry decision. Returns the booked position or None (nothing filled)."""
        underlying = template["underlying"]
        self.pending_entries.add(underlying)  # reserved synchronously by the caller's slot check
        try:
            async with self._entry_lock:
                await self.refresh()
                failures = self.entry_safety_failures(underlying=underlying, quote=quote, quantity=quantity,
                                                      decision_option=decision_option, readiness=readiness, now=now,
                                                      quote_fresh=quote_fresh)
                if failures:
                    self._journal("entry_blocked_by_execution_safety", underlying=underlying,
                                  option=quote.symbol, failures=failures)
                    print(f"LIVE NO ENTRY {underlying} {quote.symbol}: {', '.join(failures)}", flush=True)
                    return None
                outcome = await self.execute_order(
                    option_symbol=quote.symbol, action="Buy to Open", quantity=quantity, limit=quote.ask,
                    fill_timeout=self.settings.entry_fill_timeout_seconds,
                    intent={"purpose": "entry", "underlying": underlying, "position_template": template})
                if outcome.filled_quantity <= 0 or outcome.average_fill_price is None:
                    if outcome.confirmed:
                        self._clear_intent(outcome.external_identifier)
                    print(f"LIVE ENTRY NOT FILLED {underlying} {quote.symbol}: {outcome.status} {outcome.detail}", flush=True)
                    return None
                record = self._book_entry(template, outcome.filled_quantity, outcome.average_fill_price, outcome.fees,
                                          outcome.order_id, outcome.external_identifier)
                self.save()
                if outcome.confirmed:
                    self._clear_intent(outcome.external_identifier)
                else:
                    self._set_intent(outcome.external_identifier, booked_quantity=outcome.filled_quantity)
                print(f"LIVE ENTRY {underlying} {quote.symbol} x{outcome.filled_quantity}/{quantity} "
                      f"@ ${outcome.average_fill_price:.2f} order={outcome.order_id}", flush=True)
                await asyncio.to_thread(self.refresh_broker_state, force=True)
                return record
        finally:
            self.pending_entries.discard(underlying)

    async def close_position(self, *, record: dict, reason: str, now: datetime,
                             requote: Callable[[dict], OptionQuote],
                             quote_fresh: Callable[[OptionQuote, datetime], bool]) -> float | None:
        """Execute the strategy's exit decision with limit orders at the live bid, re-pricing only after a
        confirmed cancel. Returns realized P/L when fully closed, None when any quantity remains open."""
        key = f"exit:{record['trade_id']}"
        realized_total = 0.0
        if not await asyncio.to_thread(self._settle_intents_for, record["trade_id"]):
            return None  # an earlier exit order for this position is still unresolved; never stack another
        for attempt in range(1, self.settings.exit_max_attempts + 1):
            if record not in (self.account.positions if self.account else []):
                break
            broker_qty = await asyncio.to_thread(self.broker_quantity, record["symbol"])
            if broker_qty is None:
                self.block_entries(key, "could not read broker position before exit")
                return None
            quantity = min(int(record["quantity"]), broker_qty)
            if quantity <= 0:
                self.block_entries(key, f"ledger holds {record['quantity']} of {record['symbol']} but broker holds {broker_qty}")
                return None
            quote = await asyncio.to_thread(requote, record)
            if quote.bid <= 0 or not quote_fresh(quote, datetime.now(timezone.utc)):
                self._journal("exit_quote_unusable", trade_id=record["trade_id"], option=record["symbol"],
                              bid=quote.bid, attempt=attempt)
                await self._sleep(self.settings.poll_interval_seconds * 2)
                continue
            outcome = await self.execute_order(
                option_symbol=record["symbol"], action="Sell to Close", quantity=quantity, limit=quote.bid,
                fill_timeout=self.settings.exit_fill_timeout_seconds,
                intent={"purpose": "exit", "trade_id": record["trade_id"], "reason": reason})
            if outcome.filled_quantity > 0 and outcome.average_fill_price is not None:
                realized_total += self._book_exit(record, outcome.filled_quantity, outcome.average_fill_price,
                                                  outcome.fees, reason, datetime.now(timezone.utc),
                                                  outcome.order_id, outcome.external_identifier)
                self.save()
                print(f"LIVE EXIT {record['underlying']} {record['symbol']} {reason} x{outcome.filled_quantity} "
                      f"@ ${outcome.average_fill_price:.2f} order={outcome.order_id}", flush=True)
            if not outcome.confirmed:
                self._set_intent(outcome.external_identifier, booked_quantity=outcome.filled_quantity)
                self.block_entries(key, f"exit order {outcome.order_id} for {record['symbol']} unresolved ({outcome.status})")
                return None  # entries are blocked by execute_order; never stack a second order on an unknown one
            self._clear_intent(outcome.external_identifier)
            if record not in self.account.positions:
                self.unblock_entries(key)
                await asyncio.to_thread(self.refresh_broker_state, force=True)
                return realized_total
        if record in self.account.positions:
            self.block_entries(key, f"exit '{reason}' for {record['symbol']} not completed after "
                                    f"{self.settings.exit_max_attempts} attempts; position still open")
            record_critical_alert("live_exit_incomplete", mode="LIVE",
                                  details={"trade_id": record["trade_id"], "option_symbol": record["symbol"],
                                           "reason": reason}, data_dir=self.data_dir)
            return None
        return realized_total
