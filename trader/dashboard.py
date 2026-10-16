from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .config import AppConfig
from .metrics import calculate_performance


def create_app(data_dir: str | Path = "data", root: str | Path = "."):
    try:
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse
    except ImportError as exc:
        raise RuntimeError("Install dashboard extra: pip install -e '.[dashboard]'") from exc
    data, root = Path(data_dir), Path(root)
    app = FastAPI(title="OptioBot local dashboard", docs_url=None, redoc_url=None)

    def read(path: Path, default: Any):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): return default

    def account_profile() -> dict:
        """The isolated account-profile PAPER ledger (separate from the original ledger shown below)."""
        from .account_profiles import ProfileConfig, ProfileError, ProfileLedger
        try:
            ledger = ProfileLedger(ProfileConfig.load(root, "small"), original_data_dir=data)
            status = ledger.status()
        except (ProfileError, OSError, KeyError, ValueError) as exc:
            return {"configured": False, "detail": f"{type(exc).__name__}: {exc}"}
        status.pop("universe", None)
        return {"configured": True, **status}

    @app.get("/api/profile")
    def profile():
        return account_profile()

    @app.get("/api/status")
    def status():
        account = read(data / "paper_account.json", {})
        decisions_path = data / "decisions.jsonl"
        decisions = []
        if decisions_path.exists():
            decisions = [json.loads(line) for line in decisions_path.read_text(encoding="utf-8").splitlines()[-100:] if line.strip()]
        trades = account.get("trades", [])
        perf = calculate_performance(trades).__dict__
        manifest = read(root / "active_strategy.json", {})
        return {"mode": os.getenv("TRADING_MODE", "PAPER"), "live_warning": os.getenv("TRADING_MODE", "PAPER").upper() == "LIVE",
                "account": account, "performance": perf, "strategy": manifest, "last_decision": decisions[-1] if decisions else None,
                "data_freshness": "unknown" if not decisions else decisions[-1].get("diagnostics", {}).get("data_age_seconds"),
                "broker_connectivity": "not checked", "reconciliation": "not checked",
                "daily_loss_status": "see decisions", "pending_proposals": len(list((root / "strategy_change_requests").glob("*.json"))) if (root / "strategy_change_requests").exists() else 0,
                "review_status": read(root / "agent_status.json", {}),
                "account_profile": account_profile()}

    @app.get("/", response_class=HTMLResponse)
    def home():
        return """<!doctype html><meta charset=utf-8><title>OptioBot</title>
<style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;background:#101820;color:#eee}h1{color:#75d6a3}.live{background:#b00020;padding:1.5rem;font-size:1.5rem;font-weight:bold}.card{background:#1c2a35;padding:1rem;margin:.7rem 0;border-radius:8px}pre{white-space:pre-wrap}.profile{border:2px solid #75d6a3}.pname{font-size:1.3rem;font-weight:bold;color:#75d6a3;margin-bottom:.5rem}td{padding:.15rem .8rem .15rem 0}</style>
<h1>OptioBot</h1><div id=mode></div><div id=profile></div><div id=cards></div><script>fetch('/api/status').then(r=>r.json()).then(s=>{
document.getElementById('mode').innerHTML='<div class="'+(s.live_warning?'live':'card')+'">MODE: '+s.mode+(s.live_warning?' — REAL MONEY LIVE TRADING':'')+'</div>';
const p=s.account_profile||{};const m=v=>v==null?'—':'$'+Number(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
document.getElementById('profile').innerHTML=!p.configured||!p.initialized?'<div class=card><b>account profile</b> not initialized</div>':
'<div class="card profile"><div class=pname>PROFILE: '+p.active_profile+' (PAPER, ledger '+p.ledger_id+')</div><table>'+
[['Starting ledger',m(p.starting_balance)],['Current equity',m(p.equity)],['Available cash',m(p.cash)],
['Open max loss (aggregate)',m(p.open_max_loss)],['Max permitted allocation',p.max_aggregate_allocation==null?'original settings (no extra cap)':m(p.max_aggregate_allocation)+' ('+Math.round(p.allocation_fraction*100)+'%)'],
['Remaining risk budget',p.remaining_risk_budget==null?'—':m(p.remaining_risk_budget)],['Symbols scanned',p.symbols_scanned],
['Qualifying signals today',p.signals_today],['Affordable candidates today',p.affordable_candidates_today],
['Rejected today',p.rejected_today+' '+JSON.stringify(p.rejection_reasons_today)],
['Progress to '+m(p.transition_threshold),m(p.verified_equity_for_transition)+' ('+(p.progress_to_threshold*100).toFixed(1)+'%)'],
['Transitions',p.transitions.length?JSON.stringify(p.transitions):'none']].map(r=>'<tr><td>'+r[0]+'</td><td><b>'+r[1]+'</b></td></tr>').join('')+'</table></div>';
document.getElementById('cards').innerHTML=['account','performance','strategy','last_decision','data_freshness','broker_connectivity','reconciliation','daily_loss_status','pending_proposals','review_status'].map(k=>'<div class=card><b>'+k+'</b><pre>'+JSON.stringify(s[k],null,2)+'</pre></div>').join('');});</script>"""
    return app
