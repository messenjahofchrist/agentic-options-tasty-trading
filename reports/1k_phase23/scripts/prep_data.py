# Inputs for the proxy: daily closes (Massive daily aggregates, Apr 1 - Nov 6) and DXLink 1-minute bars Oct 12 - Nov 6.
import asyncio, json, os, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from trader.config import AppConfig, load_dotenv
from trader.tastytrade_client import TastytradeClient
from trader.v2.datahub import history_batch

OUT = Path(__file__).parent
SYMS = ["SPY", "QQQ", "IWM", "NVDA", "AAPL", "AMZN", "GOOGL", "PLTR", "NFLX", "INTC", "UBER"]
load_dotenv(); cfg = AppConfig.from_env(); key = os.environ["MASSIVE_API_KEY"]
daily = {}
for s in SYMS:
    url = f"https://api.massive.com/v2/aggs/ticker/{s}/range/1/day/2026-04-01/2026-11-06?adjusted=true&sort=asc&limit=500&apiKey={key}"
    for attempt in range(5):
        try:
            rows = json.loads(urllib.request.urlopen(url, timeout=30).read()).get("results", [])
            daily[s] = [(datetime.fromtimestamp(r["t"] / 1000, timezone.utc).date().isoformat(), r["c"]) for r in rows]
            break
        except Exception as exc:
            time.sleep(15)
    print(s, len(daily.get(s, [])), flush=True)
    time.sleep(13)
(OUT / "daily_closes.json").write_text(json.dumps(daily), encoding="utf-8")
client = TastytradeClient(cfg.client_id, cfg.client_secret, cfg.refresh_token, base_url=cfg.production_base_url)
tok = client.quote_token()["data"]
got, failed = asyncio.run(history_batch(dxlink_url=tok["dxlink-url"], quote_token=tok["token"],
                                        series={s: f"{s}{{=1m,a=s,tho=true}}" for s in SYMS},
                                        start=datetime(2026, 10, 12, tzinfo=timezone.utc), timeout_seconds=240))
(OUT / "minute_bars_oct.json").write_text(json.dumps(got), encoding="utf-8")
print("minute bars", {s: len(v) for s, v in got.items()}, failed)
