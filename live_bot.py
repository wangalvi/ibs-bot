#!/usr/bin/env python3
"""
IBS-OR-MACD paper-trading bot for Alpaca.

Strategy (unchanged from the backtest):
  IBS  = (close - low) / (high - low) for the day.
  IBS signal: turns ON when IBS < 0.20, stays on until a day with IBS > 0.80.
  MACD signal: ON while MACD(12, 26) is above its 9-day signal line.
  Be fully invested in the symbol if EITHER signal is on; otherwise hold cash.

How a trading day works:
  1. Run the bot any time before the close (Task Scheduler can start it in the morning).
  2. It waits until 15 minutes before the close (3:45 pm ET normally, earlier on half-days).
  3. It estimates today's IBS and MACD from the live price, decides in/out,
     and sends a MARKET-ON-CLOSE order only if the position needs to change.
  4. Every decision is logged to logs/decisions.csv and logs/bot.log.

Small accounts (fractional shares):
  Alpaca allows fractional shares only on regular "day" orders, not market-on-close orders.
  order_mode "auto" (default) uses whole-share market-on-close orders when the budget buys at least
  5 shares, and otherwise switches to FRACTIONAL mode: a dollar-amount market order at 3:55 pm ET
  (5 minutes before the close). Set "order_mode" to "moc" or "fractional" to force one.

Safety:
  - Paper trading only, unless you deliberately enable live trading (see LIVE TRADING below).
  - Create a file named STOP in this folder to make the bot do nothing.
  - Refuses to trade on stale or suspicious prices.

Setup:
  pip install alpaca-py pandas
  Put your PAPER keys in a file named alpaca_keys.env in this folder:
      APCA_API_KEY_ID=your_key_id
      APCA_API_SECRET_KEY=your_secret_key
  Never share this file or paste the keys anywhere.

Usage:
  python live_bot.py --dry-run --now     # test: compute today's decision immediately, place no order
  python live_bot.py                     # normal: wait for 15 min before the close, then trade
  python live_bot.py --report            # show the log and account vs. buy-and-hold since the start
"""
import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
ET = ZoneInfo("America/New_York")

DEFAULTS = dict(symbol="QQQ", capital_fraction=0.98, max_capital=None, minutes_before_close=15,
                order_mode="auto", fractional_minutes_before_close=5,
                ibs_buy=0.20, ibs_sell=0.80, macd_fast=12, macd_slow=26, macd_signal=9)

# LIVE TRADING: only if you set live=true in bot_config.json AND set the environment variable
# ALLOW_LIVE_TRADING=yes. Paper trade for months first.


# --------------------------------------------------------------------------------------
# Strategy logic (pure functions, shared with the self-test)
# --------------------------------------------------------------------------------------
def ibs_series(high, low, close):
    rng = high - low
    return ((close - low) / rng).where(rng > 0, 0.5)


def ibs_state(ibs_values, buy=0.20, sell=0.80):
    """Replays the IBS on/off rule day by day. Returns the on/off state after each day."""
    out, on = [], False
    for v in ibs_values:
        if on and v > sell:
            on = False
        elif not on and v < buy:
            on = True
        out.append(on)
    return out


def macd_on(closes, fast=12, slow=26, signal=9):
    m = closes.ewm(span=fast, adjust=False).mean() - closes.ewm(span=slow, adjust=False).mean()
    s = m.ewm(span=signal, adjust=False).mean()
    return (m > s), m, s


def decide(bars, cfg):
    """bars: daily DataFrame (open/high/low/close), last row = today (live estimate).
    Returns a dict with the signals for the last row."""
    ibs = ibs_series(bars["high"], bars["low"], bars["close"])
    state = ibs_state(ibs.values, cfg["ibs_buy"], cfg["ibs_sell"])
    mon, m, s = macd_on(bars["close"], cfg["macd_fast"], cfg["macd_slow"], cfg["macd_signal"])
    return dict(ibs=float(ibs.iloc[-1]), ibs_on=bool(state[-1]), ibs_on_yesterday=bool(state[-2]),
                macd=float(m.iloc[-1]), macd_signal=float(s.iloc[-1]), macd_on=bool(mon.iloc[-1]),
                invest=bool(state[-1] or mon.iloc[-1]))


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding="utf-8"),
                                  logging.StreamHandler(sys.stdout)])


def load_keys():
    path = os.path.join(HERE, "alpaca_keys.env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key, secret = os.environ.get("APCA_API_KEY_ID"), os.environ.get("APCA_API_SECRET_KEY")
    if not key or not secret:
        sys.exit("No API keys found. Create alpaca_keys.env in this folder (see the instructions at the top of this file).")
    return key, secret


def load_config():
    cfg = dict(DEFAULTS); cfg["live"] = False
    path = os.path.join(HERE, "bot_config.json")
    if os.path.exists(path):
        cfg.update(json.load(open(path, encoding="utf-8")))
    return cfg


def log_decision(row):
    path = os.path.join(LOG_DIR, "decisions.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------------------------------
# Alpaca
# --------------------------------------------------------------------------------------
def clients(cfg):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    key, secret = load_keys()
    paper = True
    if cfg.get("live"):
        if os.environ.get("ALLOW_LIVE_TRADING") != "yes":
            sys.exit("bot_config.json says live=true, but ALLOW_LIVE_TRADING=yes is not set. Refusing to trade real money.")
        paper = False
    return TradingClient(key, secret, paper=paper), StockHistoricalDataClient(key, secret), paper


def history(data, symbol, days=400):
    """Daily bars up to yesterday (split-adjusted, so they line up with today's live prices)."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    end = dt.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - dt.timedelta(days=int(days * 1.5))
    last_err = None
    for feed in (DataFeed.SIP, DataFeed.IEX):          # free accounts can read SIP history older than 15 minutes
        try:
            req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end,
                                   adjustment=Adjustment.SPLIT, feed=feed)
            df = data.get_stock_bars(req).df
            if len(df):
                df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
                df.index = pd.to_datetime(df.index).tz_convert(ET).normalize().tz_localize(None)
                logging.info(f"history: {len(df)} daily bars from {feed.value} feed, last {df.index[-1].date()}")
                return df[["open", "high", "low", "close", "volume"]]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"could not load daily history: {last_err}")


def live_today(data, symbol):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockSnapshotRequest
    last_err = None
    for feed in (DataFeed.SIP, DataFeed.IEX):          # SIP needs a paid data plan; IEX is free
        try:
            snap = data.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol, feed=feed))[symbol]
            bar, trade = snap.daily_bar, snap.latest_trade
            return dict(open=float(bar.open), high=float(bar.high), low=float(bar.low), price=float(trade.price),
                        trade_time=trade.timestamp, bar_date=bar.timestamp.astimezone(ET).date(), feed=feed.value)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"could not get a live snapshot: {last_err}")


def current_qty(trading, symbol):
    try:
        return float(trading.get_open_position(symbol).qty)
    except Exception:  # noqa: BLE001  (no position)
        return 0.0


def cancel_open_orders(trading, symbol):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest
    for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
        trading.cancel_order_by_id(o.id)
        logging.info(f"cancelled earlier open order {o.id}")


def submit_moc(trading, symbol, qty, side):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    req = MarketOrderRequest(symbol=symbol, qty=int(qty), side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                             time_in_force=TimeInForce.CLS)
    return trading.submit_order(req)


def choose_mode(trading, cfg, symbol):
    """'moc' (whole shares at the closing auction) when the budget is big enough to buy ~5+ shares,
    otherwise 'fractional' (dollar-amount market order 5 minutes before the close)."""
    mode = cfg.get("order_mode", "auto")
    if mode in ("moc", "fractional"):
        return mode
    budget = float(trading.get_account().equity) * cfg["capital_fraction"]
    if cfg.get("max_capital"):
        budget = min(budget, float(cfg["max_capital"]))
    return "moc" if budget >= cfg.get("moc_min_budget", 3500) else "fractional"


def submit_fractional(trading, symbol, side, dollars):
    """Dollar-amount market order (time in force = day). Sells close the whole position."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest
    if side == "sell":
        return trading.close_position(symbol)
    asset = trading.get_asset(symbol)
    if not getattr(asset, "fractionable", False):
        raise RuntimeError(f"{symbol} is not fractionable on Alpaca")
    return trading.submit_order(MarketOrderRequest(symbol=symbol, notional=dollars, side=OrderSide.BUY,
                                                   time_in_force=TimeInForce.DAY))


# --------------------------------------------------------------------------------------
# One trading day
# --------------------------------------------------------------------------------------
def run_day(args, cfg):
    if os.path.exists(os.path.join(HERE, "STOP")):
        logging.warning("STOP file present - doing nothing."); return
    trading, data, paper = clients(cfg)
    sym = cfg["symbol"]
    logging.info(f"==== {sym} bot ({'PAPER' if paper else 'LIVE'}){' DRY RUN' if args.dry_run else ''} ====")

    clock = trading.get_clock()
    now = clock.timestamp.astimezone(ET)
    if not clock.is_open and not args.now:
        nxt_open = clock.next_open.astimezone(ET)
        if nxt_open.date() != now.date():
            logging.info(f"market is not open today (next open {nxt_open:%Y-%m-%d %H:%M} ET) - nothing to do."); return
        logging.info(f"waiting for the open at {nxt_open:%H:%M} ET")
    close_t = clock.next_close.astimezone(ET)
    mode = choose_mode(trading, cfg, sym)
    mins = cfg["fractional_minutes_before_close"] if mode == "fractional" else cfg["minutes_before_close"]
    act_at = close_t - dt.timedelta(minutes=mins)
    logging.info(f"order mode: {mode} (decides {mins} min before the close)")
    if not args.now:
        if now > act_at:
            logging.warning(f"too late: it is {now:%H:%M} ET and the decision time was {act_at:%H:%M} ET. Skipping today."); return
        wait = (act_at - now).total_seconds()
        if args.max_wait and wait > args.max_wait * 60:
            logging.info(f"decision time {act_at:%H:%M} ET is {wait / 60:.0f} min away (> --max-wait {args.max_wait}); "
                         "a later scheduled run will handle today.")
            return
        logging.info(f"close is {close_t:%H:%M} ET; sleeping {wait / 60:.0f} min until {act_at:%H:%M} ET")
        time.sleep(max(wait, 0))

    hist = history(data, sym)
    live = live_today(data, sym)
    today = dt.datetime.now(ET).date()
    # ---- sanity checks ----
    age_min = (dt.datetime.now(dt.timezone.utc) - live["trade_time"]).total_seconds() / 60
    prev_close = float(hist["close"].iloc[-1])
    problems = []
    if live["bar_date"] != today and not args.now:
        problems.append(f"live bar is dated {live['bar_date']}, not today")
    if age_min > 15 and not args.now:
        problems.append(f"latest trade is {age_min:.0f} min old")
    if abs(live["price"] / prev_close - 1) > 0.12:
        problems.append(f"price {live['price']:.2f} is >12% from yesterday's close {prev_close:.2f}")
    if hist.index[-1].date() >= today:
        hist = hist.iloc[:-1]                      # never double-count today
    if problems:
        logging.error("NOT TRADING - " + "; ".join(problems)); return

    bars = pd.concat([hist, pd.DataFrame([dict(open=live["open"], high=max(live["high"], live["price"]),
                                               low=min(live["low"], live["price"]), close=live["price"], volume=np.nan)],
                                         index=[pd.Timestamp(today)])])
    sig = decide(bars, cfg)

    acct = trading.get_account()
    equity = float(acct.equity)
    qty = current_qty(trading, sym)
    budget = equity * cfg["capital_fraction"]
    if cfg.get("max_capital"):
        budget = min(budget, float(cfg["max_capital"]))
    if mode == "fractional":
        target = round(budget / live["price"], 6) if sig["invest"] else 0.0
    else:
        target = math.floor(budget / live["price"]) if sig["invest"] else 0
    if sig["invest"] and qty > 0:
        target = qty                               # already invested: hold, don't churn
    delta = target - qty
    action = "hold" if abs(delta) < 1e-9 else ("buy" if delta > 0 else "sell")
    if mode == "fractional" and action == "buy" and budget < 1.0:
        logging.warning("budget under $1 - nothing to buy"); action = "hold"

    logging.info(f"price {live['price']:.2f} ({live['feed']}), today's range {bars['low'].iloc[-1]:.2f}-{bars['high'].iloc[-1]:.2f}, "
                 f"IBS {sig['ibs']:.2f} -> IBS signal {'ON' if sig['ibs_on'] else 'off'}; "
                 f"MACD {sig['macd']:.3f} vs signal {sig['macd_signal']:.3f} -> {'ON' if sig['macd_on'] else 'off'}")
    logging.info(f"decision: {'INVESTED' if sig['invest'] else 'CASH'} | holding {qty:g} sh, target {target:g} sh -> {action.upper()} {abs(delta):g}"
                 + (f" (about ${abs(delta) * live['price']:,.2f})" if action != "hold" else ""))

    order_id = ""
    if action != "hold" and not args.dry_run:
        try:
            cancel_open_orders(trading, sym)
            if mode == "fractional":
                o = submit_fractional(trading, sym, action, round(min(budget, abs(delta) * live["price"]), 2))
            else:
                o = submit_moc(trading, sym, abs(delta), action)
            order_id = str(o.id)
            kind = "fractional market" if mode == "fractional" else "market-on-close"
            logging.info(f"submitted {kind} {action} of {abs(delta):g} {sym}: order {order_id}, status {getattr(o, 'status', '')}")
        except Exception as e:  # noqa: BLE001
            logging.error(f"ORDER FAILED: {e}")
            order_id = f"FAILED: {e}"
    log_decision(dict(date=str(today), time_et=dt.datetime.now(ET).strftime("%H:%M:%S"), mode="paper" if paper else "LIVE",
                      dry_run=args.dry_run, symbol=sym, price=round(live["price"], 4), feed=live["feed"],
                      order_mode=mode, ibs=round(sig["ibs"], 4), ibs_on=sig["ibs_on"], macd_on=sig["macd_on"], invest=sig["invest"],
                      equity=round(equity, 2), qty_before=qty, target_qty=target, action=action, order_id=order_id))


def report(cfg):
    path = os.path.join(LOG_DIR, "decisions.csv")
    if not os.path.exists(path):
        print("No decisions logged yet."); return
    d = pd.read_csv(path)
    d = d[d.dry_run.astype(str).str.lower() != "true"]
    if d.empty:
        print("Only dry runs so far."); return
    print(d.tail(15).to_string(index=False))
    first, last = d.iloc[0], d.iloc[-1]
    bot = last.equity / first.equity - 1
    bh = last.price / first.price - 1
    print(f"\nSince {first.date}: account {bot * 100:+.2f}%  vs  {cfg['symbol']} buy & hold {bh * 100:+.2f}% "
          f"(prices at decision time; final fills happen at the close)")
    print(f"Days invested: {d.invest.astype(str).str.lower().eq('true').mean() * 100:.0f}% | orders placed: {(d.action != 'hold').sum()}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="compute and log the decision, but place no order")
    ap.add_argument("--now", action="store_true", help="don't wait for the pre-close time (for testing)")
    ap.add_argument("--report", action="store_true", help="summarize the decision log")
    ap.add_argument("--max-wait", type=float, default=0, help="exit (do nothing) if the decision time is more than this many minutes away; used by the GitHub schedule")
    args = ap.parse_args()
    setup_logging(); cfg = load_config()
    if args.report:
        report(cfg); return
    try:
        run_day(args, cfg)
    except Exception as e:  # noqa: BLE001
        logging.exception(f"bot error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
