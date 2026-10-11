# IDX Stock Intelligence & Alert System

Serverless, dual-engine tracker for the Indonesia Stock Exchange (IDX), run entirely on GitHub Actions.

| Engine | Schedule | Output |
|---|---|---|
| `morning_brief.py` | 08:30 WIB (01:30 UTC), Mon–Fri | 🌅 Gemini-synthesized pre-market brief |
| `price_sentry.py` | Every 15 min, IDX sessions, Mon–Fri | 🚨 Buy / 🎯 Sell / 📉 RSI-oversold alerts |

## Files

```
watchlist.json                     Tickers (no .JK suffix), sector, buy_below / sell_above
morning_brief.py                   RSS + yfinance + Gemini -> Telegram
price_sentry.py                    RSI(14) / SMA(20) + price levels -> Telegram (de-duplicated)
requirements.txt                   Python 3.12+ dependencies
.github/workflows/morning_brief.yml
.github/workflows/price_sentry.yml
```

## Setup

1. **Telegram bot:** message [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the token.
   Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and copy `message.chat.id`.
2. **Gemini key:** create one at [Google AI Studio](https://aistudio.google.com/apikey).
3. **GitHub secrets:** in the repo go to *Settings → Secrets and variables → Actions → New repository secret*:

   | Secret | Value |
   |---|---|
   | `BOT_TOKEN` | Telegram bot token |
   | `CHAT_ID` | Telegram chat ID |
   | `GEMINI_API_KEY` | Gemini API key |

4. *(Optional)* Gemini models: the brief uses `gemini-3.8-flash` and falls back to `gemini-3.1-pro-preview`.
   To override either one without editing code, add repository **variables** (not secrets) named
   `GEMINI_MODEL` / `GEMINI_FALLBACK_MODEL`. Set the fallback to `none` to disable it.
5. Push the repo. Both workflows start on their cron schedules automatically.

## Manual testing

From the **Actions** tab, choose a workflow and click **Run workflow**:

- `dry_run`: prints messages to the job log instead of sending them to Telegram.
- `force_run` (Price Sentry only): skips the market-hours and stale-data checks.

### Running locally (Python 3.12+)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

$env:DRY_RUN = "1"; $env:GEMINI_API_KEY = "<key>"
.\.venv\Scripts\python morning_brief.py

$env:DRY_RUN = "1"; $env:FORCE_RUN = "1"
.\.venv\Scripts\python price_sentry.py

# Offline logic tests (alert rules, de-dup log, market hours, brief formatting)
.\.venv\Scripts\python -m unittest discover -s tests -v
```

## Customizing the watchlist

Edit `watchlist.json`. Each entry supports `ticker`, `sector`, `buy_below`, `sell_above`, and `stop_loss`.
- `buy_below`: triggers 🚨 `BUY ALERT` when price drops below target.
- `sell_above`: triggers 🎯 `SELL ALERT` (take profit) when price rises above target.
- `stop_loss`: triggers 🛑 `STOP LOSS ALERT` when price drops below stop loss (suppresses buy alert).
The global RSI oversold level is `_meta.rsi_oversold` (default `30`).

## How alerts stay de-duplicated

`price_sentry.py` writes `YYYY-MM-DD|TICKER.JK_ALERTTYPE` lines to `daily_alerts.log`.
An alert type fires at most once per ticker per trading day. Entries from earlier days are cleared automatically.
GitHub runners are ephemeral, so the workflow carries the log between runs with a date-scoped Actions cache key.

## Notes

- **Bisnis.com** blocks non-browser clients (HTTP 403), so its headlines come from Google News RSS filtered to `bisnis.com`.
- GitHub cron can start 5–15 minutes late. The sentry keeps a 10-minute grace window after each session closes (`SESSION_GRACE_MINUTES`).
- If Gemini is unavailable, the morning brief falls back to a raw data snapshot so you still get a message.
- Scheduled workflows on public repos are paused after 60 days with no repo activity. Re-enable them from the Actions tab.
