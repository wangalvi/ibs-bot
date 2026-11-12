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

## Second bot: QLD (2x Nasdaq-100) using QQQ's signals

- Settings: `bot_config_qld.json` (`symbol: QLD`, `signal_symbol: QQQ`, logs in `logs_qld/`).
- Schedule: `.github/workflows/bot_qld.yml` (same times as the QQQ bot).
- Keys: repository secrets `QLD_APCA_API_KEY_ID` and `QLD_APCA_API_SECRET_KEY`, from a second Alpaca paper account, so its money never mixes with the QQQ bot's.
- Expect bigger swings: in backtests since 2008 it lost about 62% in late 2008 and about 42% in late 2018.
