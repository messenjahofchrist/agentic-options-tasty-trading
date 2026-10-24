"""CLI commands for PROFESSIONAL_STRATEGY_V2: validate, activate, readiness, report, replay, compare.
Every command here is read-only against tastytrade (quotes, candles, metrics, chains); none can place an order."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import MANIFEST_FILE, V2ConfigError, load_v2_config, verify_v2_manifest, write_v2_manifest

ET = ZoneInfo("America/New_York")


def _universes(root: Path) -> dict[str, list[str]]:
    from ..account_profiles import ProfileConfig
    from ..strategy_loader import parse_strategy
    original = list(parse_strategy(root / "strategy.txt").universe)
    small = list(ProfileConfig.load(root, "small").small_universe)
    return {"ORIGINAL_ACCOUNT_PAPER": original, "SMALL_ACCOUNT_PAPER": small}


def validate(root: Path) -> dict[str, Any]:
    from ..strategy_loader import verify_manifest
    config = load_v2_config(root)
    verify_manifest(root / "strategy.txt", root / "active_strategy.json")
    universes = _universes(root)
    from .sectors import SectorMap
    sector_map = SectorMap(config.sector_map)
    unmapped = {name: sector_map.unmapped(symbols) for name, symbols in universes.items()}
    if any(unmapped.values()):
        raise V2ConfigError(f"sector map is missing symbols: {unmapped}")
    try:
        manifest = verify_v2_manifest(root, config)
        manifest_status = {"activated": True, "activated_at_utc": manifest.get("activated_at_utc"), "note": manifest.get("note")}
    except V2ConfigError as exc:
        manifest_status = {"activated": False, "detail": str(exc)}
    return {"strategy_id": config.strategy_id, "version": config.version, "configuration_hash": config.sha256,
            "base_strategy": {"version": config.base.version, "configuration_hash": config.base.sha256,
                              "manifest_verified": True},
            "universes": {k: len(v) for k, v in universes.items()}, "context_symbols": list(config.context_symbols()),
            "calendar_valid_through": config.calendar["calendar_valid_through"],
            "management": {k: v["enabled"] for k, v in config.data["management"].items()},
            "v2_manifest": manifest_status, "mode": "PAPER", "live_enabled": False}


def _check(report: dict, name: str, ok: bool | None, detail: Any, *, kind: str, critical: bool = True) -> None:
    status = "PASS" if ok else ("WARN" if ok is None or not critical else "FAIL")
    report["checks"].append({"name": name, "status": status, "kind": kind, "critical": critical, "detail": detail})


def readiness(root: Path, cfg, *, stream: bool = True) -> dict[str, Any]:
    """Monday acceptance checks. 'offline' checks read local files; 'integration' checks call tastytrade
    read-only endpoints. A mocked unit test never counts as an integration check here."""
    from ..calendar import MarketCalendar
    from ..market_data import ProductionMarketData
    from ..paper_runner import MAX_CLOCK_OFFSET_SECONDS
    from ..tastytrade_client import TastytradeClient, set_request_observer
    from .correlation import CorrelationModel
    from .datahub import DataHub, RequestLog
    from .options import evaluate_option, fetch_option_quotes_cached
    from .sectors import SectorMap

    now = datetime.now(timezone.utc)
    report: dict[str, Any] = {"generated_at_utc": now.isoformat(), "checks": [], "mode": cfg.mode.value}
    try:
        summary = validate(root)
        _check(report, "v2_config_valid_and_pinned_to_unchanged_base_strategy", True, summary, kind="offline")
        _check(report, "v2_manifest_activated", summary["v2_manifest"]["activated"] or None, summary["v2_manifest"],
               kind="offline", critical=False)
    except Exception as exc:
        _check(report, "v2_config_valid_and_pinned_to_unchanged_base_strategy", False, f"{type(exc).__name__}: {exc}", kind="offline")
        report["overall"] = "BLOCKED"
        return report
    config = load_v2_config(root)
    _check(report, "paper_only_live_disabled", cfg.mode.value == "PAPER" and not cfg.live_enabled,
           {"TRADING_MODE": cfg.mode.value, "ENABLE_LIVE_TRADING": cfg.live_enabled,
            "v2_live_path": "paper_runner._run refuses v2 with live execution"}, kind="offline")
    valid_through = datetime.fromisoformat(config.calendar["calendar_valid_through"]).date()
    today = now.astimezone(ET).date()
    from .events import EventEngine
    events = EventEngine(config.calendar, config.data["events"], next_session=lambda d: None)
    _check(report, "economic_calendar_current", valid_through >= today + timedelta(days=7),
           {"valid_through": valid_through.isoformat(), "next_7_days": events.upcoming(now, 7)}, kind="offline")
    from ..account_profiles import ProfileConfig
    small = ProfileConfig.load(root, "small")
    ledgers = {"ORIGINAL_ACCOUNT_PAPER": cfg.data_dir / "paper_account.json", "SMALL_ACCOUNT_PAPER": small.data_dir / "paper_account.json"}
    ledger_info = {}
    for name, path in ledgers.items():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            ledger_info[name] = {"path": str(path), "starting_capital": data["starting_capital"], "cash": data["cash"],
                                 "open_positions": len(data["positions"])}
        except (OSError, ValueError, KeyError) as exc:
            ledger_info[name] = {"path": str(path), "error": f"{type(exc).__name__}"}
    isolated = cfg.data_dir.resolve() != small.data_dir.resolve() and all("error" not in v for v in ledger_info.values())
    _check(report, "two_isolated_paper_ledgers", isolated, ledger_info, kind="offline")

    # ---- integration (tastytrade, read-only) ----------------------------------------------------------
    cfg.require_market_data_credentials()
    log = RequestLog(cfg.data_dir / "v2_readiness_requests.jsonl")
    set_request_observer(log)
    client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
    try:
        token = client.quote_token().get("data", {})
        _check(report, "tastytrade_authentication_and_quote_token", bool(token.get("token") and token.get("dxlink-url")),
               {"dxlink_url_present": bool(token.get("dxlink-url"))}, kind="integration")
    except Exception as exc:
        _check(report, "tastytrade_authentication_and_quote_token", False, f"{type(exc).__name__}: {exc}"[:300], kind="integration")
        report["overall"] = "BLOCKED"
        return report
    try:
        offset = client.clock_offset_seconds()
        _check(report, "local_clock_within_tolerance", abs(offset) <= MAX_CLOCK_OFFSET_SECONDS,
               {"offset_seconds": round(offset, 3), "limit": MAX_CLOCK_OFFSET_SECONDS}, kind="integration")
    except Exception as exc:
        _check(report, "local_clock_within_tolerance", False, str(exc)[:300], kind="integration")
    universes = _universes(root)
    all_symbols = list(dict.fromkeys([*universes["SMALL_ACCOUNT_PAPER"], *universes["ORIGINAL_ACCOUNT_PAPER"],
                                      *config.context_symbols()]))
    calendar = MarketCalendar()
    bounds = calendar.session_bounds(today)
    market_open = calendar.is_open(now)
    hub = DataHub(client=client, shared_dir=root / "data" / "v2_shared_cache", request_log=log)
    # quotes (freshness meaningful only while the market is open)
    try:
        quotes = ProductionMarketData(client).snapshots(list(config.context_symbols()))
        ages = {q.symbol: round((now - q.timestamp).total_seconds(), 1) for q in quotes}
        _check(report, "context_symbol_quotes", len(quotes) == len(config.context_symbols()) and
               (not market_open or all(a <= 60 for a in ages.values())), {"age_seconds": ages}, kind="integration")
    except Exception as exc:
        _check(report, "context_symbol_quotes", False, str(exc)[:300], kind="integration")
    try:
        vix = hub.vix(config.data["context"]["volatility_index"], max_age_seconds=0)
        _check(report, "vix_index_quote", vix is not None, vix, kind="integration")
    except Exception as exc:
        _check(report, "vix_index_quote", False, str(exc)[:300], kind="integration")
    try:
        metrics = hub.market_metrics(all_symbols, max_age_seconds=0)
        singles = [s for s in all_symbols if SectorMap(config.sector_map).is_single_stock(s)]
        with_iv = [s for s, m in metrics.items() if m.get("tw-implied-volatility-index-rank") not in (None, "")]
        earnings = {s: (metrics[s].get("earnings") or {}).get("expected-report-date") for s in singles if s in metrics}
        week = [f"{s} {d} {(metrics[s]['earnings'] or {}).get('time-of-day')}" for s, d in earnings.items()
                if d and today <= datetime.fromisoformat(d).date() <= today + timedelta(days=10)]
        _check(report, "market_metrics_iv_and_earnings", len(metrics) == len(all_symbols) and len(with_iv) >= len(all_symbols) - 2,
               {"returned": len(metrics), "requested": len(all_symbols), "with_iv_rank": len(with_iv),
                "earnings_next_10_days": week, "missing": [s for s in all_symbols if s not in metrics]}, kind="integration")
    except Exception as exc:
        _check(report, "market_metrics_iv_and_earnings", False, str(exc)[:300], kind="integration")
    sessions = int(config.data["context"]["daily_history_sessions"])

    async def history_checks() -> dict[str, Any]:
        out: dict[str, Any] = {}
        daily, failures = await hub.daily_history(all_symbols + [config.data["context"]["volatility_index"]],
                                                  dxlink_url=token["dxlink-url"], quote_token=token["token"],
                                                  sessions=sessions, today=today)
        out["daily"] = (daily, failures)
        if bounds and now >= bounds[0].astimezone(timezone.utc) + timedelta(minutes=2):
            from ..history_providers import TastytradeDXLinkHistoryProvider
            provider = TastytradeDXLinkHistoryProvider(dxlink_url=token["dxlink-url"], quote_token=token["token"])
            end = min(now, bounds[1].astimezone(timezone.utc)).replace(second=0, microsecond=0) - timedelta(minutes=1)
            batch = await provider.fetch(symbol="SPY", start=bounds[0].astimezone(timezone.utc), end=end)
            out["intraday"] = len(batch.candles)
            pre, pre_fail = await hub.premarket(list(config.data["context"]["broad_symbols"]), dxlink_url=token["dxlink-url"],
                                                quote_token=token["token"], day=today,
                                                start_et=config.data["context"]["premarket_start_et"])
            out["premarket"] = (pre, pre_fail)
        if stream and market_open:
            from ..market_data import live_minute_bars
            counts: dict[str, int] = {}
            try:
                async with asyncio.timeout(140):
                    async for symbol, bar in live_minute_bars(token["dxlink-url"], token["token"], list(config.context_symbols())):
                        counts[symbol] = counts.get(symbol, 0) + 1
                        if len(counts) == len(config.context_symbols()):
                            break
            except TimeoutError:
                pass
            out["stream"] = counts
        return out
    try:
        results = asyncio.run(history_checks())
        daily, failures = results["daily"]
        short = {s: len(r) for s, r in daily.items() if len(r) < int(config.data["correlation"]["min_observations"]) + 1}
        _check(report, "daily_history_for_levels_and_correlation", not failures and not short,
               {"symbols": len(daily), "failures": failures, "short_history": short}, kind="integration")
        model = CorrelationModel(config.data["correlation"], SectorMap(config.sector_map),
                                 {s: [(r["session_date"], r["close"]) for r in rows] for s, rows in daily.items()})
        sample = [model.pair(a, b)["method"] for a in universes["ORIGINAL_ACCOUNT_PAPER"] for b in universes["ORIGINAL_ACCOUNT_PAPER"] if a < b]
        _check(report, "correlation_measured_not_fallback", sample.count("measured") == len(sample),
               {"pairs": len(sample), "measured": sample.count("measured"),
                "example_QQQ_NVDA": model.pair("QQQ", "NVDA"), "example_SPY_TLT": model.pair("SPY", "TLT")}, kind="integration")
        if "intraday" in results:
            _check(report, "intraday_1m_history_spy", results["intraday"] > 0, {"bars": results["intraday"]}, kind="integration")
            pre, pre_fail = results["premarket"]
            _check(report, "premarket_range", not pre_fail and all(v.get("high") for v in pre.values()), pre, kind="integration")
        else:
            _check(report, "intraday_1m_history_spy", None, "market not open long enough today; checked again at startup",
                   kind="integration", critical=False)
        if "stream" in results:
            missing = [s for s in config.context_symbols() if s not in results["stream"]]
            _check(report, "dxlink_live_trade_stream_context_symbols", not missing,
                   {"bars_received": results["stream"], "missing": missing}, kind="integration")
        else:
            _check(report, "dxlink_live_trade_stream_context_symbols", None,
                   "market closed or --skip-stream: live stream not probed", kind="integration", critical=False)
    except Exception as exc:
        _check(report, "daily_history_for_levels_and_correlation", False, f"{type(exc).__name__}: {exc}"[:300], kind="integration")
    try:
        spot = ProductionMarketData(client).snapshots(["SPY"])[0]
        mid = (spot.bid + spot.ask) / 2
        chain = fetch_option_quotes_cached(client, hub, underlying="SPY", spot=mid, now=now,
                                           min_dte=int(config.base.rules["min_dte"]), max_dte=int(config.base.rules["max_dte"]))
        greeks = sum(1 for q in chain if q.delta is not None and q.gamma is not None and q.theta is not None)
        fresh = [round((now - q.quote_timestamp).total_seconds(), 1) for q in chain if q.quote_timestamp]
        example = min(chain, key=lambda q: abs(abs(q.delta or 0) - 0.55)) if chain else None
        quality = evaluate_option(example, hub.market_metrics(["SPY"], 1800).get("SPY"), datetime.now(timezone.utc),
                                  config.data["option_quality"]) if example else None
        _check(report, "option_quotes_current_with_greeks", bool(chain) and greeks >= len(chain) * 0.9 and
               (not market_open or (fresh and max(fresh) <= 60)),
               {"contracts": len(chain), "with_greeks": greeks, "max_quote_age_seconds": max(fresh) if fresh else None,
                "example_contract": example.symbol if example else None, "example_quality": quality}, kind="integration")
    except Exception as exc:
        _check(report, "option_quotes_current_with_greeks", False, f"{type(exc).__name__}: {exc}"[:300], kind="integration")
    set_request_observer(None)
    report["request_counts"] = log.counts
    failures = [c["name"] for c in report["checks"] if c["status"] == "FAIL" and c["critical"]]
    report["overall"] = "READY" if not failures else "BLOCKED"
    report["blockers"] = failures
    return report


def run_command(args, cfg, root: Path) -> int:
    if args.command == "v2-validate":
        print(json.dumps(validate(root), indent=2))
        return 0
    if args.command == "v2-activate":
        summary = validate(root)
        if not args.approve:
            print(json.dumps({"validated": summary, "activated": False,
                              "next_step": "Re-run with --approve to write " + MANIFEST_FILE}, indent=2))
            return 0
        config = load_v2_config(root)
        path = write_v2_manifest(root, config, approved_by="owner (v2-activate --approve)", note=args.note)
        verify_v2_manifest(root, config)
        print(json.dumps({"activated": True, "manifest": str(path), "configuration_hash": config.sha256}, indent=2))
        return 0
    if args.command == "v2-readiness":
        report = readiness(root, cfg, stream=not args.skip_stream)
        out = cfg.data_dir / "v2_readiness"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        if report["overall"] != "READY":
            from ..notifications import email_configured, push_configured, send_email, send_push
            text = f"V2 readiness BLOCKED: {', '.join(report.get('blockers') or [])}. Report: {path}"
            try:
                if email_configured():
                    send_email("OptioBot V2 readiness BLOCKED", text)
                if push_configured():
                    send_push("OptioBot V2 readiness BLOCKED", text[:300])
            except Exception as exc:
                print(f"readiness alert not sent: {type(exc).__name__}: {exc}")
        print(json.dumps({"overall": report["overall"], "blockers": report.get("blockers"),
                          "checks": [(c["name"], c["status"], c["kind"]) for c in report["checks"]],
                          "report": str(path)}, indent=2, default=str))
        return 0 if report["overall"] == "READY" else 3
    if args.command == "v2-report":
        from .report import write_dashboard
        print(write_dashboard(root, cfg.data_dir))
        return 0
    if args.command == "v2-replay":
        from .replay import run_replay
        print(json.dumps(run_replay(root, cfg, source=args.source, in_sample_end=args.in_sample_end, out=Path(args.out)),
                         indent=2, default=str)[:20000])
        return 0
    if args.command == "v2-compare":
        from .compare import compare_ledgers
        print(json.dumps(compare_ledgers(root, cfg.data_dir), indent=2, default=str))
        return 0
    raise ValueError(args.command)
