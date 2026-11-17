#!/usr/bin/env python3
"""Checks that day_trader.py's live decision code makes the same decisions as the backtest.
Replays QQQ 1-minute history day by day through the SAME functions the live bot uses
(noise_sigma, bands, vwap, day_decision) and compares with an independent array-based backtest.
Usage: python test_day_trader.py path/to/QQQ_1min.csv.gz [n_days]"""
import sys
import numpy as np
import pandas as pd
import day_trader as d

path = sys.argv[1]; n_days = int(sys.argv[2]) if len(sys.argv) > 2 else 250
m = pd.read_csv(path, parse_dates=[0], index_col=0)
m.columns = [c.lower() for c in m.columns]
m.index = m.index.tz_localize(d.ET) if m.index.tz is None else m.index.tz_convert(d.ET)
m = d.regular_session(m)
days = sorted(set(m.index.date))
full = [x for x in days if (m.index.date == x).sum() >= 380]
by_day = {x: g for x, g in m.groupby(m.index.date)}

# --- independent backtest (array version, same as research/fable intraday_momo.py)
P = m.pivot_table(index=m.index.date, columns=d.minute_of_day(m.index), values="close", aggfunc="last").reindex(columns=range(390)).ffill(axis=1)
P = P.loc[full]
Opn = m.groupby(m.index.date).open.first().loc[full]
mv = (P.div(Opn, axis=0) - 1).abs()
s_tab = mv.rolling(14).mean().shift(1)

mismatch = total = 0
test_days = full[-n_days:]
for x in test_days:
    i = full.index(x)
    if i < 15: continue
    hist = pd.concat([by_day[y] for y in full[i - 14:i]])
    prev = by_day[full[i - 1]]; today = by_day[x]
    prev_close = float(prev.close.iloc[-1]); o = float(today.open.iloc[0])
    pos_live = pos_bt = 0
    for k in range(30, 390 - 29, 30):
        at = today.index[0] + pd.Timedelta(minutes=k)
        bars = today[today.index < at]
        price = float(bars.close.iloc[-1])
        # live code path
        s = d.noise_sigma(hist, k)
        up, lo = d.bands(o, prev_close, s)
        pos_live = d.day_decision(pos_live, price, up, lo, d.vwap(bars))
        # backtest path
        sb = s_tab.loc[x, k - 1]
        ub, lb_ = max(o, prev_close) * (1 + sb), min(o, prev_close) * (1 - sb)
        tp = (bars.high + bars.low + bars.close) / 3; vw = float((tp * bars.volume).sum() / bars.volume.sum())
        if pos_bt == 1 and price < max(ub, vw): pos_bt = 0
        elif pos_bt == -1 and price > min(lb_, vw): pos_bt = 0
        if pos_bt == 0:
            pos_bt = 1 if price > ub else -1 if price < lb_ else 0
        total += 1; mismatch += pos_live != pos_bt
print(f"{total} checks over {len(test_days)} days: live code and backtest disagree on {mismatch}")
sys.exit(1 if mismatch else 0)
