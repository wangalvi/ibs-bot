#!/usr/bin/env python3
"""
QLD bot + intraday TQQQ/SQQQ day trades, in one Alpaca account.

Overnight part (unchanged rules, from live_bot.py):
  IBS-or-MACD on QQQ's daily candle, holding QLD, decided ~2 minutes before the close.

Day-trade part ("noise-area breakout", Zarattini, Aziz & Barbon 2024), signals from QQQ:
  noise band for minute m of the day: upper = max(today's open, prior close) x (1 + s_m)
                                      lower = min(today's open, prior close) x (1 - s_m)
  s_m = average over the prior 14 trading days of |QQQ at minute m / that day's open - 1|
  Every 30 minutes from 10:00 (last check 30 minutes before the close):
    flat  -> QQQ above upper: buy TQQQ;  QQQ below lower: buy SQQQ (the "short")
    long  -> exit when QQQ falls below max(upper, VWAP)
    short -> exit when QQQ rises above min(lower, VWAP)
  Everything is closed 5 minutes before the close. Never held overnight.
  Size: day_fraction x account equity (default 1.0), capped by the account's buying power.
  With $2,000+ equity Alpaca allows margin, so the day trade can run on top of the QLD position;
  below that it only uses whatever cash is free.

Backtest (2016-Nov 2026, QQQ 1-minute data, 0.02%/side costs): day part alone ~26%/yr (worst drop -25%);
QLD bot + day part ~89%/yr, worst drop -32% - before the slower fills and taxes of real trading.

Safety: paper only unless live=true AND ALLOW_LIVE_TRADING=yes; STOP file halts everything;
day_trading_enabled=false in the config turns the day part off; stale or odd prices skip a check.

Usage:
  python day_trader.py --config bot_config_qld.json               # run the trading day (sleeps between checks)
  python day_trader.py --config bot_config_qld.json --test        # compute the latest check now, place no orders
"""
import argparse
import csv
import datetime as dt
import logging
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd

import live_bot as lb

ET = lb.ET
CHECK_EVERY = 30          # minutes
LOOKBACK = 14             # trading days for the noise band
FLATTEN_BEFORE = 5        # minutes before the close


# --------------------------------------------------------------------------------------
# Pure strategy functions (also used by test_day_trader.py against the backtest)
# --------------------------------------------------------------------------------------
def regular_session(bars):
    """bars: minute bars indexed by ET timestamps (open/high/low/close/volume). Keeps 9:30-15:59."""
    t = bars.index.hour * 60 + bars.index.minute
    return bars[(t >= 570) & (t < 960)]


def minute_of_day(ts):
    return (ts.hour * 60 + ts.minute) - 570


def noise_sigma(hist_bars, m, lookback=LOOKBACK):
    """Average |price at minute m / open - 1| over the last `lookback` full days in hist_bars.
    'Price at minute m' = close of the bar that starts at minute m-1 (what is known at time open+m)."""
    vals = []
    for day, g in hist_bars.groupby(hist_bars.index.date):
        g = g.copy(); g["k"] = minute_of_day(g.index)
        if len(g) < 300:            # skip half days / broken days, as the backtest did
            continue
        c = g.set_index("k").close.reindex(range(390)).ffill()
        vals.append(abs(c.iloc[m - 1] / g.open.iloc[0] - 1))
    vals = vals[-lookback:]
    if len(vals) < lookback:
        raise RuntimeError(f"only {len(vals)} full days of minute history (need {lookback})")
    return float(np.mean(vals))


def vwap(today_bars):
    tp = (today_bars.high + today_bars.low + today_bars.close) / 3
    v = today_bars.volume.sum()
    return float((tp * today_bars.volume).sum() / v) if v > 0 else float(today_bars.close.iloc[-1])


def bands(today_open, prev_close, sigma):
    hi, lo = max(today_open, prev_close), min(today_open, prev_close)
    return hi * (1 + sigma), lo * (1 - sigma)


def day_decision(pos, price, upper, lower, vw):
    """pos: +1 (holding TQQQ), -1 (holding SQQQ), 0 (flat). Returns the new position."""
    if pos == 1 and price < max(upper, vw):
        pos = 0
    elif pos == -1 and price > min(lower, vw):
        pos = 0
    if pos == 0:
        if price > upper:
            pos = 1
        elif price < lower:
            pos = -1
    return pos


# --------------------------------------------------------------------------------------
# Alpaca data
# --------------------------------------------------------------------------------------
def minute_bars(data, symbol, start, end):
    """Minute bars between two ET datetimes. Free accounts can read SIP bars older than 15 minutes;
    the most recent 15 minutes come from the free IEX feed."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    def get(feed, a, b):
        req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=a, end=b,
                               adjustment=Adjustment.SPLIT, feed=feed)
        df = data.get_stock_bars(req).df
        if not len(df):
            return df
        df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
        df.index = pd.to_datetime(df.index).tz_convert(ET)
        return df[["open", "high", "low", "close", "volume"]]

    try:
        return get(DataFeed.SIP, start, end)
    except Exception:  # noqa: BLE001  (no real-time SIP subscription)
        cut = min(end, dt.datetime.now(ET) - dt.timedelta(minutes=16))
        parts = []
        if cut > start:
            parts.append(get(DataFeed.SIP, start, cut))
        if end > cut:
            parts.append(get(DataFeed.IEX, max(cut, start), end))
        parts = [p for p in parts if len(p)]
        return pd.concat(parts).sort_index() if parts else pd.DataFrame()


def latest_price(data, symbol):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockLatestTradeRequest
    for feed in (DataFeed.SIP, DataFeed.IEX):
        try:
            t = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol, feed=feed))[symbol]
            return float(t.price), t.timestamp, feed.value
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError(f"no latest trade for {symbol}")


def session(trading, day):
    from alpaca.trading.requests import GetCalendarRequest
    cal = trading.get_calendar(GetCalendarRequest(start=day, end=day))
    if not cal:
        return None
    c = cal[0]
    o = c.open if isinstance(c.open, dt.datetime) else dt.datetime.combine(c.date, c.open)
    cl = c.close if isinstance(c.close, dt.datetime) else dt.datetime.combine(c.date, c.close)
    o = o.replace(tzinfo=ET) if o.tzinfo is None else o.astimezone(ET)
    cl = cl.replace(tzinfo=ET) if cl.tzinfo is None else cl.astimezone(ET)
    return o, cl


def previous_sessions(trading, day, n):
    from alpaca.trading.requests import GetCalendarRequest
    cal = trading.get_calendar(GetCalendarRequest(start=day - dt.timedelta(days=int(n * 1.6) + 10), end=day - dt.timedelta(days=1)))
    return [c.date for c in cal][-n:]


# --------------------------------------------------------------------------------------
# Orders and logging
# --------------------------------------------------------------------------------------
LONG, SHORT = "TQQQ", "SQQQ"


def held(trading):
    q = {s: lb.current_qty(trading, s) for s in (LONG, SHORT)}
    if q[LONG] > 0 and q[SHORT] > 0:
        logging.warning("holding both TQQQ and SQQQ - will flatten both")
        return 9, q
    return (1 if q[LONG] > 0 else -1 if q[SHORT] > 0 else 0), q


def flatten(trading, dry):
    out = []
    for s in (LONG, SHORT):
        if lb.current_qty(trading, s) != 0:
            if not dry:
                lb.cancel_open_orders(trading, s)
                o = trading.close_position(s)
                out.append(f"sold all {s} ({getattr(o, 'id', '')})")
            else:
                out.append(f"(dry) would sell all {s}")
    return out


def buy(trading, symbol, dollars, dry):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    if dollars < 1:
        return "budget under $1 - skipped"
    if dry:
        return f"(dry) would buy ${dollars:,.2f} of {symbol}"
    asset = trading.get_asset(symbol)
    if getattr(asset, "fractionable", False):
        req = MarketOrderRequest(symbol=symbol, notional=round(dollars, 2), side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    else:
        px, _, _ = latest_price(lb_data[0], symbol)
        qty = int(dollars // px)
        if qty < 1:
            return f"{symbol} is not fractionable and ${dollars:,.2f} buys no whole share - skipped"
        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
    o = trading.submit_order(req)
    return f"bought ${dollars:,.2f} of {symbol} (order {o.id})"


def log_row(row):
    path = os.path.join(lb.LOG_DIR, "day_trades.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


lb_data = [None]


# --------------------------------------------------------------------------------------
# One check
# --------------------------------------------------------------------------------------
def check(trading, data, cfg, sess, at, dry, test=False):
    """Run one 30-minute check at ET datetime `at` (normally now)."""
    o_t, c_t = sess
    m = int((at - o_t).total_seconds() // 60)
    m = max(1, min(m, 389))
    day = o_t.date()
    prev_days = previous_sessions(trading, day, LOOKBACK + 6)
    hist = regular_session(minute_bars(data, "QQQ", dt.datetime.combine(prev_days[0], dt.time(9, 30), ET),
                                       dt.datetime.combine(prev_days[-1], dt.time(16, 0), ET)))
    today = regular_session(minute_bars(data, "QQQ", o_t, at))
    today = today[today.index < at]
    if hist.empty or today.empty:
        raise RuntimeError("missing minute bars")
    last_prev = hist[hist.index.date == prev_days[-1]]
    prev_close = float(last_prev.close.iloc[-1])
    today_open = float(today.open.iloc[0])
    sigma = noise_sigma(hist, m)
    upper, lower = bands(today_open, prev_close, sigma)
    vw = vwap(today)
    if test:
        price, ptime, feed = float(today.close.iloc[-1]), today.index[-1], "minute bars"
    else:
        price, ptime, feed = latest_price(data, "QQQ")
    pos, q = held(trading)
    problems = []
    age = (dt.datetime.now(dt.timezone.utc) - pd.Timestamp(ptime).tz_convert("UTC")).total_seconds() / 60
    if not test and age > 5:
        problems.append(f"latest QQQ trade is {age:.0f} min old")
    if abs(price / prev_close - 1) > 0.2:
        problems.append(f"QQQ {price:.2f} is >20% from prior close {prev_close:.2f}")
    if problems:
        logging.error("day trade check skipped - " + "; ".join(problems))
        return
    new = day_decision(0 if pos == 9 else pos, price, upper, lower, vw)
    actions = []
    if pos == 9 or new != pos:
        actions += flatten(trading, dry)
        if new != 0 and cfg.get("day_trading_enabled", True):
            time.sleep(0 if dry else 2)
            acct = trading.get_account()
            equity, bp = float(acct.equity), float(acct.buying_power)
            dollars = min(equity * cfg.get("day_fraction", 1.0), bp * 0.95)
            actions.append(buy(trading, LONG if new == 1 else SHORT, dollars, dry))
    label = {1: "LONG (TQQQ)", -1: "SHORT (SQQQ)", 0: "FLAT"}
    logging.info(f"check {at:%H:%M} ET: QQQ {price:.2f} ({feed}) | band {lower:.2f}-{upper:.2f} (s={sigma*100:.2f}%) | "
                 f"VWAP {vw:.2f} | was {label.get(pos, 'BOTH')} -> {label[new]}" + (f" | {'; '.join(actions)}" if actions else ""))
    log_row(dict(date=str(day), time_et=at.strftime("%H:%M"), dry_run=dry, qqq=round(price, 2), upper=round(upper, 2),
                 lower=round(lower, 2), vwap=round(vw, 2), sigma_pct=round(sigma * 100, 3), was=pos, now=new,
                 actions=" | ".join(actions)))


# --------------------------------------------------------------------------------------
# The trading day
# --------------------------------------------------------------------------------------
def run(args, cfg):
    if os.path.exists(os.path.join(lb.HERE, "STOP")):
        logging.warning("STOP file present - doing nothing."); return
    trading, data, paper = lb.clients(cfg)
    lb_data[0] = data
    started = time.time()
    now = dt.datetime.now(ET)
    logging.info(f"==== QLD + day-trade bot ({'PAPER' if paper else 'LIVE'}){' TEST' if args.test else ''} ====")

    if args.test:      # evaluate the most recent check time of the latest session, no orders
        day = now.date()
        sess = session(trading, day)
        if sess is None or now < sess[0] + dt.timedelta(minutes=CHECK_EVERY):
            day = previous_sessions(trading, day, 1)[-1]; sess = session(trading, day)
        o_t, c_t = sess
        last_check = o_t + dt.timedelta(minutes=CHECK_EVERY * int(((min(now, c_t) - o_t).total_seconds() // 60 - 1) // CHECK_EVERY))
        last_check = min(last_check, c_t - dt.timedelta(minutes=CHECK_EVERY))
        check(trading, data, cfg, sess, last_check, dry=True, test=True)
        lb.run_day(SimpleNamespace(dry_run=True, now=True, max_wait=0), cfg)
        return

    sess = session(trading, now.date())
    if sess is None:
        logging.info("market closed today - nothing to do."); return
    o_t, c_t = sess
    if now >= c_t:
        logging.info("today's session is over."); return
    checks = []
    k = 1
    while o_t + dt.timedelta(minutes=CHECK_EVERY * k) <= c_t - dt.timedelta(minutes=CHECK_EVERY):
        checks.append(o_t + dt.timedelta(minutes=CHECK_EVERY * k)); k += 1
    flat_t = c_t - dt.timedelta(minutes=FLATTEN_BEFORE)
    qld_t = c_t - dt.timedelta(minutes=cfg.get("fractional_minutes_before_close", 2))
    events = [("check", t) for t in checks] + [("flatten", flat_t), ("qld", qld_t)]

    # catch up: if we started after a check time, run the latest missed check right away (positions are read from the account)
    past = [t for kind, t in events if kind == "check" and t <= now and now < flat_t]
    upcoming = [(kind, t) for kind, t in events if t > now]
    if past:
        upcoming.insert(0, ("check", now))
    if not upcoming:
        return
    first_wait = (upcoming[0][1] - now).total_seconds() / 60
    if args.max_wait and first_wait > args.max_wait:
        logging.info(f"next event at {upcoming[0][1]:%H:%M} ET is {first_wait:.0f} min away (> --max-wait {args.max_wait}); a later run will handle it.")
        return
    for kind, t in upcoming:
        if args.max_minutes and (t - dt.datetime.now(ET)).total_seconds() / 60 + (time.time() - started) / 60 > args.max_minutes:
            logging.info(f"stopping before {kind} at {t:%H:%M} ET to stay inside the run-time limit; the next scheduled run continues.")
            return
        wait = (t - dt.datetime.now(ET)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        try:
            if kind == "check":
                check(trading, data, cfg, sess, max(t, dt.datetime.now(ET)), dry=args.dry_run)
            elif kind == "flatten":
                acts = flatten(trading, args.dry_run)
                logging.info(f"{t:%H:%M} ET end-of-day: " + ("; ".join(acts) if acts else "no day-trade position"))
                if acts:
                    log_row(dict(date=str(o_t.date()), time_et=t.strftime("%H:%M"), dry_run=args.dry_run, qqq="", upper="",
                                 lower="", vwap="", sigma_pct="", was="", now=0, actions=" | ".join(acts)))
            else:
                time.sleep(5)       # let the end-of-day sells settle into buying power
                lb.run_day(SimpleNamespace(dry_run=args.dry_run, now=False, max_wait=0), cfg)
        except Exception as e:  # noqa: BLE001  (one bad check must not kill the day)
            logging.exception(f"{kind} at {t:%H:%M} failed: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="bot_config_qld.json")
    ap.add_argument("--dry-run", action="store_true", help="decide and log, but place no orders")
    ap.add_argument("--test", action="store_true", help="evaluate the latest check right now (no orders) and exit")
    ap.add_argument("--max-wait", type=float, default=0, help="exit if the first event is more than this many minutes away")
    ap.add_argument("--max-minutes", type=float, default=0, help="stop before any event that would exceed this run time")
    args = ap.parse_args()
    cfg = lb.load_config(args.config)
    lb.LOG_DIR = os.path.join(lb.HERE, cfg.get("log_dir", "logs"))
    lb.setup_logging()
    try:
        run(args, cfg)
    except Exception as e:  # noqa: BLE001
        logging.exception(f"bot error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
