#!/usr/bin/env python3
"""Offline test of the netting logic in day_trader.py against a simulated broker (no network, no orders).
Leveraged ETFs need full cash (buying power = cash), fills are instant at fixed prices."""
import datetime as dt
import types
import day_trader as d
import live_bot as lb

d.time.sleep = lambda s: None
PRICE = {"QLD": 75.0, "TQQQ": 50.0, "SQQQ": 20.0}

class Broker:
    def __init__(self, cash, qld=0.0):
        self.cash, self.pos, self.orders, self.n = cash, {"QLD": qld, "TQQQ": 0.0, "SQQQ": 0.0}, [], 0
    def get_open_position(self, s):
        if self.pos.get(s, 0) <= 0: raise RuntimeError("no position")
        return types.SimpleNamespace(qty=str(self.pos[s]))
    def get_account(self):
        eq = self.cash + sum(q * PRICE[s] for s, q in self.pos.items())
        return types.SimpleNamespace(equity=str(eq), buying_power=str(self.cash))
    def get_asset(self, s): return types.SimpleNamespace(fractionable=True)
    def get_orders(self, req):
        syms = getattr(req, "symbols", None)
        return [o for o in self.orders if (not syms or o.symbol in syms) and (str(getattr(req, "status", "")).endswith("ALL") or o.status == "open")]
    def cancel_order_by_id(self, i): pass
    def submit_order(self, r):
        self.n += 1; px = PRICE[r.symbol]
        qty = float(r.qty) if r.qty is not None else float(r.notional) / px
        side = str(r.side).lower()
        if "buy" in side:
            assert qty * px <= self.cash + 1e-6, f"not enough cash for {r.symbol}"
            self.cash -= qty * px; self.pos[r.symbol] += qty
        else:
            self.cash += qty * px; self.pos[r.symbol] -= qty
        o = types.SimpleNamespace(id=self.n, symbol=r.symbol, client_order_id=r.client_order_id, filled_qty=str(qty), status="filled")
        self.orders.append(o); return o
    def close_position(self, s):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest
        return self.submit_order(MarketOrderRequest(symbol=s, qty=self.pos[s], side=OrderSide.SELL, time_in_force=TimeInForce.DAY))
    def exposure(self):
        eq = float(self.get_account().equity)
        return (2 * self.pos["QLD"] * PRICE["QLD"] + 3 * self.pos["TQQQ"] * PRICE["TQQQ"] - 3 * self.pos["SQQQ"] * PRICE["SQQQ"]) / eq

def step(b, new, label):
    flag = d.overnight_flag(b, dt.date.today())
    want = d.targets(flag, new, float(b.get_account().equity), 0.98)
    acts = d.rebalance(b, want, dry=False)
    print(f"{label:46s} flag={flag!s:5s} exposure {b.exposure():+.2f}x | cash ${b.cash:8.2f} | " + "; ".join(a.split(' (')[0] for a in acts))
    return b.exposure()

ok = True
print("A) QLD held overnight ($2,940 QLD + $60 cash)")
b = Broker(cash=60.0, qld=2940 / 75)
ok &= abs(step(b, -1, "  short signal -> expect -1x") + 0.98) < 0.05
ok &= abs(step(b, 0, "  short ends -> expect QLD back, ~+2x") - 1.96) < 0.05
ok &= abs(step(b, 1, "  long signal -> expect +3x (TQQQ)") - 2.94) < 0.05
ok &= abs(step(b, -1, "  flips to short -> expect -1x") + 0.98) < 0.05
print("  3:55 flatten:", d.flatten(b, dry=False), f"-> exposure {b.exposure():+.2f}x (3:58 decision re-buys QLD)")
ok &= abs(b.exposure()) < 1e-9
print("B) no QLD overnight ($3,000 cash)")
b = Broker(cash=3000.0)
ok &= abs(step(b, -1, "  short signal -> expect -3x") + 2.94) < 0.05
ok &= abs(step(b, 0, "  short ends -> expect flat (no QLD)")) < 1e-9
ok &= abs(step(b, 1, "  long signal -> expect +3x") - 2.94) < 0.05
print("ALL TESTS PASSED" if ok else "TEST FAILED"); raise SystemExit(0 if ok else 1)
