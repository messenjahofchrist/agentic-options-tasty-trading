import json, sys, os, glob
import numpy as np, pandas as pd
S = os.path.dirname(os.path.abspath(__file__))
def load(sym):
    z = np.load(os.path.join(S, sym + ".npz"))
    return pd.DataFrame({k: z[k] for k in z.files})
def dayfeat(sym):
    df = load(sym)
    g = df.groupby("day")
    d = pd.DataFrame({"open": g.o.first(), "high": g.h.max(), "low": g.l.min(), "close": g.c.last(), "vol": g.v.sum()})
    orr = df[df.minute < 15].groupby("day")
    d["or_hi"] = orr.h.max(); d["or_lo"] = orr.l.min(); d["or_vol"] = orr.v.sum()
    d["or_close"] = orr.c.last()
    pc = d.close.shift(1)
    tr = np.maximum(d.high - d.low, np.maximum((d.high - pc).abs(), (d.low - pc).abs()))
    d["atr14"] = tr.rolling(14, min_periods=8).mean().shift(1)
    d["prev_close"] = pc; d["prev_high"] = d.high.shift(1); d["prev_low"] = d.low.shift(1)
    d["gap"] = d.open / pc - 1
    d["rvol_or"] = d.or_vol / d.or_vol.rolling(14, min_periods=8).mean().shift(1)
    d["or_w_atr"] = (d.or_hi - d.or_lo) / d.atr14
    d["or_w_pct"] = (d.or_hi - d.or_lo) / d.open
    d["sma20"] = d.close.rolling(20, min_periods=10).mean().shift(1)
    d["or_dir"] = np.sign(d.or_close - d.open)
    d["sym"] = sym
    return d.reset_index(), df
if __name__ == "__main__":
    syms = [os.path.basename(p)[:-4] for p in glob.glob(os.path.join(S, "*.npz"))]
    out = pd.concat([dayfeat(s)[0] for s in sorted(syms)])
    out.to_pickle(os.path.join(S, "dayfeat.pkl")); print(out.shape); print(out[out.sym=="SPY"].tail(3).T)
