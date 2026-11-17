# IBS OR MACD paper bot on GitHub Actions

This bot trades QQQ on Alpaca once per trading day, near the close. It runs on GitHub's servers, so your PC can be off. It trades paper money only: `"live": false` in `bot_config.json`, and the `ALLOW_LIVE_TRADING` safety gate is never set here.

## How it runs

- **Schedule:** `.github/workflows/bot.yml` starts the bot at four times each weekday (UTC). The bot exits right away unless its decision time is within the next hour.
  - That covers summer and winter time and 1 pm half-days.
  - Only one run per day actually trades.
- **Decision time:**
  - Small paper or live balances (under about $3,500): a fractional dollar order at 3:55 pm ET.
  - Larger balances: a whole-share market-on-close order, placed at 3:45 pm ET.
- **Keys:** the Alpaca keys come from repository secrets (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`). They are never stored in the code.
- **Logs:** every decision is appended to `logs/decisions.csv` and committed back to the repository. Full logs of each run are kept for 30 days under each workflow run's Artifacts.

## Test it

Go to Actions, choose "IBS MACD paper bot", then "Run workflow". Mode `test` (the default) decides immediately without placing an order; `trade` does a normal run.

## Stop it

You can stop it in either of two ways:

- Actions → "IBS MACD paper bot" → the "..." menu → Disable workflow.
- Commit an empty file named `STOP` to the repository.

## Second bot: QLD overnight + TQQQ/SQQQ day trades (QQQ signals)

Replaces the plain QLD bot. One Alpaca paper account, run by `day_trader.py`:
- **Overnight:** the same IBS-or-MACD rule on QQQ's daily candle, holding QLD, decided at 3:58 pm ET.
- **Day trades:** every 30 minutes from 10:00 ET, if QQQ breaks out of its usual range for that time of day
  (based on the last 14 days), buy TQQQ (upside) or SQQQ (downside). Exit when QQQ falls back past VWAP or the range edge.
  All day trades are closed at 3:55 pm ET, never held overnight.
- Settings: `bot_config_qld.json` (`day_trading_enabled`, `day_fraction` = share of equity per day trade).
  Logs: `logs_qld/decisions.csv` (overnight) and `logs_qld/day_trades.csv` (every check).
- Schedule: `.github/workflows/bot_qld.yml` (one run carries the whole day; it stays running during market hours).
- Keys: repository secrets `QLD_APCA_API_KEY_ID` and `QLD_APCA_API_SECRET_KEY`, from a second Alpaca paper account.
  Set that paper account to $2,000 or more so margin lets the day trade run on top of the QLD position.
- `python test_day_trader.py QQQ_1min.csv.gz` checks that the live code matches the backtest decision for decision.
- Backtest 2016 - Nov 2026: ~89%/yr before costs of real fills and taxes, worst drop -32%. Overnight QLD can still
  lose most of its value in a 2000- or 2008-style crash; the day trades do not protect overnight holdings.
