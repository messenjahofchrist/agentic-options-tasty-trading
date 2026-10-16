"""Account profiles: an isolated PAPER ledger whose account allocation and scanning universe differ from the
original account, while the strategy itself (strategy.txt, its hash and every analytical rule) is unchanged.

SMALL_ACCOUNT_PAPER     owner-authorized 2026-10-16: $1,000 ledger, 40% aggregate open-position cap, expanded
                        scanning universe (profiles/small_account_universe.json).
ORIGINAL_ACCOUNT_PAPER  the exact original account settings recorded before the profile existed
                        (docs/profiles/baseline_20261016/original_account_settings.json): the strategy's own
                        universe and per-trade sizing, no extra cap.

SMALL_ACCOUNT_PAPER also uses an owner-authorized daily loss limit (risk_overrides in the profile file, 20%
since 2026-10-16) in place of the strategy's 2%; ORIGINAL restores the strategy's own value.

The profile never edits the strategy. Per decision cycle it hands the unchanged strategy a per-trade sizing
budget equal to the remaining aggregate allocation (owner choice, 2026-10-16: one trade may use what is left
of the 40% pool). Signals, filters, contract selection, exits and loss limits are the strategy's own.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_utils import append_jsonl, atomic_replace
from .notifications import email_configured, send_email
from .portfolio import PaperAccount, load_or_initialize
from .strategy_loader import Strategy

SMALL = "SMALL_ACCOUNT_PAPER"
ORIGINAL = "ORIGINAL_ACCOUNT_PAPER"
PROFILE_FILES = {"small": "profiles/small_account.json"}
_TICKER = re.compile(r"[A-Z.]{1,8}")


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProfileConfig:
    ledger_id: str
    data_dir: Path
    starting_balance: float
    small_allocation_fraction: float
    small_universe: tuple[str, ...]
    transition_equity: float
    baseline: dict[str, Any]
    config_path: Path
    rest_requests_per_second: float | None = None
    small_risk_overrides: tuple[tuple[str, float], ...] = ()

    @classmethod
    def load(cls, root: Path, name_or_path: str) -> "ProfileConfig":
        path = root / PROFILE_FILES.get(name_or_path, name_or_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"Cannot read account profile {path}: {exc}") from exc
        small = data["profiles"][SMALL]
        universe_data = json.loads((root / small["universe_file"]).read_text(encoding="utf-8"))
        universe = tuple(dict.fromkeys(universe_data["symbols"]))
        if not universe or any(not isinstance(s, str) or not _TICKER.fullmatch(s) for s in universe):
            raise ProfileError("expanded universe must be a non-empty list of uppercase tickers")
        overrides = {str(k): float(v) for k, v in (small.get("risk_overrides") or {}).items()}
        if set(overrides) - {"max_daily_loss_fraction"}:
            raise ProfileError("only max_daily_loss_fraction may be overridden by an account profile")
        if any(not 0 < v <= 1 for v in overrides.values()):
            raise ProfileError("risk override fractions must be in (0, 1]")
        fraction = float(small["max_aggregate_allocation_fraction"])
        if not 0 < fraction <= 1:
            raise ProfileError("max_aggregate_allocation_fraction must be in (0, 1]")
        if data["profiles"][ORIGINAL].get("restore") != "original_account_baseline":
            raise ProfileError(f"{ORIGINAL} must restore the recorded original_account_baseline")
        baseline = json.loads((root / data["original_account_baseline"]).read_text(encoding="utf-8"))
        if data.get("mode", "PAPER") != "PAPER" or small.get("mode") != "PAPER" or data["profiles"][ORIGINAL].get("mode") != "PAPER":
            raise ProfileError("account profiles are PAPER-only")
        data_dir = Path(data["data_dir"])
        return cls(ledger_id=str(data["ledger_id"]), data_dir=data_dir if data_dir.is_absolute() else root / data_dir,
                   starting_balance=float(data["starting_balance"]), small_allocation_fraction=fraction,
                   small_universe=universe, transition_equity=float(data["transition"]["equity_threshold"]),
                   baseline=baseline, config_path=path,
                   rest_requests_per_second=float(data["rest_requests_per_second"]) if data.get("rest_requests_per_second") else None,
                   small_risk_overrides=tuple(sorted(overrides.items())))


def verify_original_baseline(strategy: Strategy, baseline: dict[str, Any]) -> None:
    """The original profile restores recorded values; refuse if the running strategy is not that strategy."""
    problems = []
    if strategy.sha256 != baseline.get("configuration_hash"):
        problems.append(f"strategy hash {strategy.sha256[:12]} != recorded {str(baseline.get('configuration_hash'))[:12]}")
    if list(strategy.universe) != baseline.get("universe"):
        problems.append("strategy universe differs from the recorded original universe")
    if dict(strategy.risk) != baseline.get("risk"):
        problems.append("strategy risk settings differ from the recorded original risk settings")
    if problems:
        raise ProfileError("Original settings cannot be established confidently: " + "; ".join(problems))


class AccountProfile:
    """Runtime allocation policy handed to the shared loop. Exposure = maximum potential loss of each open long
    option: full premium x 100 x quantity plus modeled round-trip costs (the strategy's modeled_risk_dollars)."""

    def __init__(self, name: str, allocation_fraction: float | None):
        self.name = name
        self.allocation_fraction = allocation_fraction

    @staticmethod
    def position_max_loss(position: dict) -> float:
        modeled = position.get("modeled_risk_dollars")
        if modeled is not None:
            return float(modeled)
        return float(position["entry_price"]) * 100 * int(position["quantity"]) + float(position.get("entry_fees", 0))

    def open_exposure(self, account: PaperAccount) -> float:
        return sum(self.position_max_loss(p) for p in account.positions)

    def allocation_cap(self, equity: float) -> float | None:
        return None if self.allocation_fraction is None else max(0.0, equity) * self.allocation_fraction

    def remaining(self, equity: float, account: PaperAccount) -> float | None:
        cap = self.allocation_cap(equity)
        return None if cap is None else max(0.0, cap - self.open_exposure(account))

    def cycle_strategy(self, strategy: Strategy, equity: float, account: PaperAccount) -> Strategy:
        """The unchanged strategy, with its per-trade sizing budget set to the remaining aggregate allocation."""
        remaining = self.remaining(equity, account)
        if remaining is None:
            return strategy
        fraction = remaining / equity if equity > 0 else 0.0
        return replace(strategy, risk={**strategy.risk, "max_risk_per_trade_fraction": fraction,
                                       "hard_max_risk_per_trade_fraction": fraction})

    def entry_allowed(self, account: PaperAccount, equity: float, modeled_risk: float) -> tuple[bool, str]:
        cap = self.allocation_cap(equity)
        if cap is None:
            return True, "no_aggregate_cap"
        exposure = self.open_exposure(account)
        if exposure + float(modeled_risk) > cap + 1e-9:
            return False, (f"aggregate_allocation_cap: open max loss ${exposure:,.2f} + this ${float(modeled_risk):,.2f} "
                           f"> ${cap:,.2f} ({self.allocation_fraction:.0%} of ${equity:,.2f})")
        return True, "within_aggregate_cap"


class ProfileLedger:
    """The isolated ledger, its persistent profile state and the one-way $10,000 transition."""

    def __init__(self, config: ProfileConfig, *, original_data_dir: Path):
        self.config = config
        if config.data_dir.resolve() == Path(original_data_dir).resolve():
            raise ProfileError("profile ledger directory must differ from the original PAPER data directory")
        self.data_dir = config.data_dir
        self.account_path = self.data_dir / "paper_account.json"
        self.state_path = self.data_dir / "profile_state.json"
        self.audit_path = self.data_dir / "profile_audit.jsonl"

    # --- state ---------------------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        atomic_replace(temporary, self.state_path)

    def _audit(self, event: str, **fields: Any) -> None:
        append_jsonl(self.audit_path, {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "event": event,
                                       "ledger_id": self.config.ledger_id, **fields})

    def initialize(self) -> dict[str, Any]:
        """Create the ledger at exactly the configured balance, or verify the existing one is this ledger."""
        state = self.state()
        if not state:
            if self.account_path.exists():
                raise ProfileError(f"{self.account_path} exists without profile state; refusing to adopt an unknown ledger")
            account = load_or_initialize(self.account_path, self.config.starting_balance)
            state = {"ledger_id": self.config.ledger_id, "ledger_uuid": str(uuid.uuid4()),
                     "created_at_utc": datetime.now(timezone.utc).isoformat(),
                     "starting_balance": account.starting_capital, "active_profile": SMALL, "transitions": []}
            self._save_state(state)
            self._audit("ledger_initialized", starting_balance=account.starting_capital, active_profile=SMALL)
        if state.get("ledger_id") != self.config.ledger_id:
            raise ProfileError(f"profile state belongs to ledger {state.get('ledger_id')}, not {self.config.ledger_id}")
        account = self.account()
        if abs(account.starting_capital - float(state["starting_balance"])) > 1e-9:
            raise ProfileError("profile ledger starting capital does not match its recorded starting balance")
        return state

    def account(self) -> PaperAccount:
        if not self.account_path.exists():
            raise ProfileError("profile ledger has not been initialized")
        return load_or_initialize(self.account_path, self.config.starting_balance)

    @property
    def active_profile(self) -> str:
        return self.state().get("active_profile", SMALL)

    # --- equity & transition ---------------------------------------------------------------------
    @staticmethod
    def verified_equity(account: PaperAccount) -> float:
        """Transition equity: settled ledger cash, with any open position valued at zero. Only booked fills
        move it, so a quote spike or a bad mark cannot trigger the transition (flatten-at-close leaves no
        open positions at a session boundary, where cash equals net liquidation value)."""
        return float(account.cash)

    def check_transition(self, *, boundary: str, now: datetime | None = None) -> dict[str, Any] | None:
        """Called only at session boundaries. One way: once ORIGINAL is active it never reverts."""
        state = self.initialize()
        if state["active_profile"] != SMALL:
            return None
        account = self.account()
        equity = self.verified_equity(account)
        if equity < self.config.transition_equity:
            return None
        now = now or datetime.now(timezone.utc)
        record = {"from": SMALL, "to": ORIGINAL, "at_utc": now.isoformat(), "boundary": boundary,
                  "verified_equity": equity, "threshold": self.config.transition_equity,
                  "open_positions_preserved": len(account.positions), "ledger_rows": len(account.ledger)}
        state["active_profile"] = ORIGINAL
        state["transitions"] = [*state.get("transitions", []), record]
        self._save_state(state)
        self._audit("profile_transition", **record)
        self._notify(record)
        return record

    def _notify(self, record: dict[str, Any]) -> None:
        message = (f"OptioBot PAPER ledger '{self.config.ledger_id}' reached ${record['verified_equity']:,.2f} "
                   f"(threshold ${record['threshold']:,.2f}) at the {record['boundary']} session boundary. "
                   f"{ORIGINAL} is now active: original scanning universe and original risk/allocation settings "
                   "restored. Strategy unchanged; positions and history preserved.")
        status = "not_configured"
        if email_configured():
            try:
                send_email(f"OptioBot: {ORIGINAL} restored", message)
                status = "sent"
            except Exception as exc:  # best effort; the audit log and console are the record
                status = f"failed:{type(exc).__name__}"
        append_jsonl(self.data_dir / "notifications.jsonl", {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                                                              "event": "profile_transition", "message": message,
                                                              "email_status": status})
        print("PROFILE TRANSITION: " + message, flush=True)

    # --- what the shared loop runs -----------------------------------------------------------------
    def runtime(self, strategy: Strategy) -> tuple[Strategy, AccountProfile]:
        """(strategy with this profile's scanning universe, allocation policy). Rules are never touched."""
        verify_original_baseline(strategy, self.config.baseline)
        if self.active_profile == ORIGINAL:
            return strategy, AccountProfile(ORIGINAL, None)
        # Owner-authorized account rule (2026-10-16): the daily loss limit for this ledger. It feeds both the
        # strategy's entry block and its daily-loss exit exactly as the strategy's own value would.
        risk = {**strategy.risk, **dict(self.config.small_risk_overrides)}
        return (replace(strategy, universe=self.config.small_universe, risk=risk),
                AccountProfile(SMALL, self.config.small_allocation_fraction))

    def status(self, strategy: Strategy | None = None) -> dict[str, Any]:
        """Dashboard / CLI summary of the profile ledger and today's scanning outcome."""
        state = self.state()
        if not state:
            return {"initialized": False, "ledger_id": self.config.ledger_id}
        account = self.account()
        active = state.get("active_profile", SMALL)
        policy = AccountProfile(active, self.config.small_allocation_fraction if active == SMALL else None)
        equity = account.equity
        cap = policy.allocation_cap(equity)
        exposure = policy.open_exposure(account)
        universe = self.config.small_universe if active == SMALL else tuple(self.config.baseline["universe"])
        today = datetime.now(timezone.utc).date().isoformat()
        signals = affordable = 0
        rejections: dict[str, int] = {}
        for decisions_path in today_decision_logs(self.data_dir, today):
            for line in decisions_path.read_text(encoding="utf-8").splitlines():
                if today not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not str(row.get("timestamp", "")).startswith(today):
                    continue
                diag = row.get("diagnostics", {})
                if not diag.get("underlying_signal"):
                    continue
                signals += 1
                if row.get("decision") == "PAPER_ENTRY" or diag.get("quantity"):
                    affordable += 1
                if row.get("decision") != "PAPER_ENTRY":
                    reason = str(row.get("reason", "unknown")).split(":")[0]
                    rejections[reason] = rejections.get(reason, 0) + 1
        return {"initialized": True, "ledger_id": self.config.ledger_id, "active_profile": active, "mode": "PAPER",
                "starting_balance": float(state["starting_balance"]), "equity": round(equity, 2),
                "cash": round(account.cash, 2), "realized_pnl": round(account.realized_pnl, 2),
                "open_positions": len(account.positions), "open_max_loss": round(exposure, 2),
                "max_aggregate_allocation": None if cap is None else round(cap, 2),
                "allocation_fraction": policy.allocation_fraction,
                "remaining_risk_budget": None if cap is None else round(max(0.0, cap - exposure), 2),
                "symbols_scanned": len(universe), "universe": list(universe),
                "signals_today": signals, "affordable_candidates_today": affordable,
                "rejected_today": sum(rejections.values()), "rejection_reasons_today": rejections,
                "transition_threshold": self.config.transition_equity,
                "verified_equity_for_transition": round(self.verified_equity(account), 2),
                "progress_to_threshold": round(min(1.0, self.verified_equity(account) / self.config.transition_equity), 4),
                "transitions": state.get("transitions", []),
                "strategy_hash": strategy.sha256 if strategy else self.config.baseline.get("configuration_hash")}


def today_decision_logs(data_dir: Path, today: str) -> list[Path]:
    """decisions.jsonl plus the archives append_jsonl rotated out of it today (it rotates at 10 MB, which a
    47-symbol session reaches about hourly). Lines are written with sorted keys, so filter by parsed timestamp."""
    stamp = today.replace("-", "")
    archives = sorted(p for p in data_dir.glob("decisions.jsonl.*.archive") if p.name.split(".")[2].startswith(stamp))
    current = data_dir / "decisions.jsonl"
    return archives + ([current] if current.is_file() else [])


def run_profile_paper_session(*, root: Path, cfg, strategy: Strategy, profile_name: str = "small",
                              symbols: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Run the shared PAPER loop on the profile's isolated ledger. Transitions are checked only at the
    session boundaries (before the session starts and after it ends), never mid-session."""
    from .config import Mode
    from .paper_runner import run_paper_session
    from .tastytrade_client import set_request_rate_limit

    if cfg.mode is not Mode.PAPER:
        raise ProfileError("account profiles are PAPER-only; TRADING_MODE must be PAPER")
    config = ProfileConfig.load(root, profile_name)
    ledger = ProfileLedger(config, original_data_dir=cfg.data_dir)
    ledger.initialize()
    ledger.check_transition(boundary="session_start")
    run_strategy, policy = ledger.runtime(strategy)
    profile_cfg = replace(cfg, data_dir=ledger.data_dir, starting_cash=config.starting_balance)
    status = ledger.status(strategy)
    ledger._audit("session_start", active_profile=policy.name, symbols=len(run_strategy.universe),
                  equity=status["equity"], strategy_hash=strategy.sha256)
    cap_text = (f"aggregate open max loss <= {policy.allocation_fraction:.0%} of equity "
                f"(${status['max_aggregate_allocation']:,.2f} now)" if policy.allocation_fraction is not None
                else "original account settings (no extra cap)")
    print(f"ACCOUNT PROFILE {policy.name} | ledger {config.ledger_id} ${status['equity']:,.2f} | "
          f"{len(run_strategy.universe)} symbols | {cap_text} | strategy {strategy.version} {strategy.sha256[:12]}",
          flush=True)
    # The expanded universe sends more quote requests per minute than tastytrade accepts in one burst.
    set_request_rate_limit(config.rest_requests_per_second)
    try:
        # ORIGINAL runs with profile=None: byte-for-byte the original account behaviour, on this ledger.
        run_paper_session(root=root, cfg=profile_cfg, strategy=run_strategy, symbols=symbols,
                          profile=policy if policy.allocation_fraction is not None else None)
    finally:
        ledger.check_transition(boundary="session_end")
        ledger._audit("session_end", active_profile=ledger.active_profile)
    return ledger.status(strategy)
