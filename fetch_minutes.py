#!/usr/bin/env python3
"""Download QQQ 1-minute and daily bars (SIP, split-adjusted) for backtest updates. Writes data_export/*.csv.
Usage: python fetch_minutes.py 2026-10-01"""
import os, sys, datetime as dt
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment, DataFeed
key, sec = os.environ["APCA_API_KEY_ID"].strip(), os.environ["APCA_API_SECRET_KEY"].strip()
c = StockHistoricalDataClient(key, sec)
start = dt.datetime.fromisoformat(sys.argv[1] if len(sys.argv) > 1 else "2026-10-01")
end = dt.datetime.utcnow() - dt.timedelta(minutes=16)
os.makedirs("data_export", exist_ok=True)
for tf, name in ((TimeFrame.Minute, "QQQ_1min"), (TimeFrame.Day, "QQQ_daily")):
    df = c.get_stock_bars(StockBarsRequest(symbol_or_symbols="QQQ", timeframe=tf, start=start, end=end,
                                           adjustment=Adjustment.ALL, feed=DataFeed.SIP)).df.xs("QQQ", level="symbol")
    df.index = df.index.tz_convert("America/New_York").tz_localize(None)
    df.to_csv(f"data_export/{name}.csv"); print(name, len(df), df.index.min(), df.index.max())
