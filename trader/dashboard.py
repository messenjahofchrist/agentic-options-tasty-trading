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
                "review_status": read(root / "agent_status.json", {})}

    @app.get("/", response_class=HTMLResponse)
    def home():
        return """<!doctype html><meta charset=utf-8><title>OptioBot</title>
<style>body{font:16px system-ui;max-width:1000px;margin:2rem auto;background:#101820;color:#eee}h1{color:#75d6a3}.live{background:#b00020;padding:1.5rem;font-size:1.5rem;font-weight:bold}.card{background:#1c2a35;padding:1rem;margin:.7rem 0;border-radius:8px}pre{white-space:pre-wrap}</style>
<h1>OptioBot</h1><div id=mode></div><div id=cards></div><script>fetch('/api/status').then(r=>r.json()).then(s=>{
document.getElementById('mode').innerHTML='<div class="'+(s.live_warning?'live':'card')+'">MODE: '+s.mode+(s.live_warning?' — REAL MONEY LIVE TRADING':'')+'</div>';
document.getElementById('cards').innerHTML=['account','performance','strategy','last_decision','data_freshness','broker_connectivity','reconciliation','daily_loss_status','pending_proposals','review_status'].map(k=>'<div class=card><b>'+k+'</b><pre>'+JSON.stringify(s[k],null,2)+'</pre></div>').join('');});</script>"""
    return app
