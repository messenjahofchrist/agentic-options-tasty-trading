import json, sys, glob, os, time
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
src = sys.argv[1]; out = sys.argv[2]
t0 = time.time()
for path in sorted(glob.glob(os.path.join(src, "massive-*-20260601T0000-20261010T0000.json"))):
    sym = os.path.basename(path).split("-")[1]
    dst = os.path.join(out, sym + ".npz")
    if os.path.exists(dst): continue
    rows = json.load(open(path))
    day, minute, o, h, l, c, v = [], [], [], [], [], [], []
    for r in rows:
        t = datetime.fromisoformat(r["timestamp"]).astimezone(ET)
        m = t.hour * 60 + t.minute
        if m < 570 or m >= 960: continue
        day.append(t.year * 10000 + t.month * 100 + t.day); minute.append(m - 570)
        o.append(r["open"]); h.append(r["high"]); l.append(r["low"]); c.append(r["close"]); v.append(r["volume"])
    del rows
    np.savez_compressed(dst, day=np.array(day), minute=np.array(minute), o=np.array(o), h=np.array(h),
                        l=np.array(l), c=np.array(c), v=np.array(v))
    print(sym, len(day), round(time.time() - t0, 1), flush=True)
