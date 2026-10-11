#!/usr/bin/env python3
"""
price_sentry.py - Intraday IDX price-level and technical alert engine.

Logic
-----
1. Pull 60 days of daily history per watchlist stock (yfinance) and compute a
   14-period RSI and a 20-period SMA with pandas_ta.
2. Static triggers:  price < buy_below  -> 🚨 BUY ALERT
                     price > sell_above -> 🎯 SELL ALERT
3. Dynamic trigger:  RSI(14) < 30       -> 📉 TECHNICAL ALERT (oversold)
4. De-duplication via `daily_alerts.log` (format: YYYY-MM-DD|TICKER.JK_ALERTTYPE).
   An alert type fires at most once per ticker per trading day; entries from a
   previous day are cleared automatically.

Schedule: every 15 minutes, 02:00-09:59 UTC (cron `*/15 2-9 * * 1-5`), with the
IDX session windows enforced in code:
    Mon-Thu  Session I 09:00-12:00 WIB | Session II 13:30-15:49 WIB
    Friday   Session I 09:00-11:30 WIB | Session II 14:00-15:49 WIB (Friday prayer break)

Usage
-----
python price_sentry.py                 # normal run
python price_sentry.py --check-market  # stdlib-only gate; writes open/today to $GITHUB_OUTPUT

Environment variables
---------------------
BOT_TOKEN               Telegram bot token   (required unless DRY_RUN)
CHAT_ID                 Telegram chat ID     (required unless DRY_RUN)
FORCE_RUN               "1" -> ignore market-hours and stale-data guards (manual testing)
DRY_RUN                 "1" -> print alerts instead of sending (log is not updated)
SESSION_GRACE_MINUTES   Minutes after a session close still treated as open (default 10,
                        absorbs GitHub Actions cron start delays)

Note: third-party libraries (pandas, yfinance, pandas_ta, requests) are imported
lazily so `--check-market` can run on a bare interpreter before `pip install`.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WIB = timezone(timedelta(hours=7), name="WIB")
BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"
ALERT_LOG_PATH = BASE_DIR / "daily_alerts.log"

HISTORY_PERIOD = "60d"
RSI_LENGTH = 14
SMA_LENGTH = 20
DEFAULT_RSI_OVERSOLD = 30.0
HTTP_TIMEOUT = 20
SESSION_GRACE_MINUTES = int(os.getenv("SESSION_GRACE_MINUTES", "10") or 10)

SESSIONS_MON_THU = ((dtime(9, 0), dtime(12, 0)), (dtime(13, 30), dtime(15, 49)))
SESSIONS_FRI = ((dtime(9, 0), dtime(11, 30)), (dtime(14, 0), dtime(15, 49)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("price_sentry")


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes"}


# --------------------------------------------------------------------------- #
# Market-hours gate (stdlib only)
# --------------------------------------------------------------------------- #
def market_status(now: datetime, grace_minutes: int = SESSION_GRACE_MINUTES) -> tuple[bool, str]:
    if now.weekday() >= 5:
        return False, "weekend"
    sessions = SESSIONS_FRI if now.weekday() == 4 else SESSIONS_MON_THU
    current = now.time()
    for idx, (start, end) in enumerate(sessions, start=1):
        end_with_grace = (datetime.combine(now.date(), end) + timedelta(minutes=grace_minutes)).time()
        if start <= current <= end_with_grace:
            return True, f"session {idx} ({start:%H:%M}-{end:%H:%M} WIB)"
    return False, "outside IDX trading sessions / lunch break"


def write_github_output(**values: str) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def to_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if not math.isnan(number) and number > 0 else None


def fmt_rp(value: float | None) -> str:
    return f"Rp {value:,.0f}" if is_num(value) else "N/A"


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def to_yahoo_symbol(ticker: str) -> str:
    ticker = ticker.strip().upper()
    return ticker if ticker.endswith(".JK") else f"{ticker}.JK"


def load_watchlist(path: Path = WATCHLIST_PATH) -> tuple[list[dict[str, Any]], float]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot read watchlist %s: %s", path, exc)
        return [], DEFAULT_RSI_OVERSOLD

    rsi_threshold = DEFAULT_RSI_OVERSOLD
    raw_stocks = data
    if isinstance(data, dict):
        raw_stocks = data.get("stocks", [])
        rsi_threshold = to_float(data.get("_meta", {}).get("rsi_oversold")) or DEFAULT_RSI_OVERSOLD

    stocks: list[dict[str, Any]] = []
    for item in raw_stocks or []:
        if not isinstance(item, dict) or not item.get("ticker"):
            log.warning("Skipping malformed watchlist entry: %r", item)
            continue
        ticker = str(item["ticker"]).strip().upper().removesuffix(".JK")
        stocks.append({**item, "ticker": ticker, "symbol": to_yahoo_symbol(ticker)})
    return stocks, rsi_threshold


# --------------------------------------------------------------------------- #
# State management (de-duplication)
# --------------------------------------------------------------------------- #
class AlertLog:
    """Line format: YYYY-MM-DD|TICKER.JK_ALERTTYPE  (e.g. 2026-10-12|ANTM.JK_price_buy)."""

    def __init__(self, path: Path, today: str) -> None:
        self.path = path
        self.today = today
        self.fired: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            self.path.touch()          # always present so CI cache-save never misses it
            return
        try:
            lines = [ln.strip() for ln in self.path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError as exc:
            log.warning("Cannot read %s (%s) - starting with an empty log.", self.path, exc)
            lines = []

        stale = False
        for line in lines:
            day, sep, key = line.partition("|")
            if sep and day == self.today and key:
                self.fired.add(key)
            else:
                stale = True
        if stale:
            log.info("Alert log contains entries from a previous day - clearing them.")
            self._rewrite()

    def _rewrite(self) -> None:
        self.path.write_text("".join(f"{self.today}|{k}\n" for k in sorted(self.fired)), encoding="utf-8")

    def has_fired(self, key: str) -> bool:
        return key in self.fired

    def record(self, key: str) -> None:
        self.fired.add(key)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(f"{self.today}|{key}\n")


# --------------------------------------------------------------------------- #
# Market data & indicators
# --------------------------------------------------------------------------- #
@dataclass
class Snapshot:
    price: float
    rsi: float | None
    sma: float | None
    bar_date: date


def _last_valid(series: Any) -> float | None:
    if series is None:
        return None
    series = series.dropna()
    return float(series.iloc[-1]) if not series.empty else None


def fetch_snapshot(symbol: str) -> Snapshot | None:
    import pandas_ta as ta          # lazy: see module docstring
    import yfinance as yf

    try:
        df = yf.Ticker(symbol).history(
            period=HISTORY_PERIOD, interval="1d", auto_adjust=False, actions=False
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance history failed for %s: %s", symbol, exc)
        return None

    if df is None or df.empty or "Close" not in df.columns:
        log.warning("No price history returned for %s", symbol)
        return None

    df = df.dropna(subset=["Close"])
    if df.empty:
        log.warning("Price history for %s contains no valid closes", symbol)
        return None

    close = df["Close"].astype(float)
    rsi = sma = None
    try:
        rsi = _last_valid(ta.rsi(close, length=RSI_LENGTH))
        sma = _last_valid(ta.sma(close, length=SMA_LENGTH))
    except Exception as exc:  # noqa: BLE001
        log.warning("Indicator calculation failed for %s: %s", symbol, exc)
    if rsi is None or sma is None:
        log.info("%s: only %d bars - RSI/SMA may be unavailable.", symbol, len(close))

    last_ts = df.index[-1]
    if getattr(last_ts, "tzinfo", None) is not None:
        last_ts = last_ts.tz_convert("Asia/Jakarta")
    return Snapshot(price=float(close.iloc[-1]), rsi=rsi, sma=sma, bar_date=last_ts.date())


# --------------------------------------------------------------------------- #
# Alert rules & message templates
# --------------------------------------------------------------------------- #
def _context_line(snap: Snapshot) -> str:
    rsi = f"{snap.rsi:.1f}" if snap.rsi is not None else "N/A"
    return f"RSI(14): {rsi} | SMA(20): {fmt_rp(snap.sma)}"


def evaluate(stock: dict[str, Any], snap: Snapshot, rsi_threshold: float) -> list[tuple[str, str]]:
    """Return a list of (alert_type, message) tuples."""
    ticker = stock["ticker"]
    alerts: list[tuple[str, str]] = []

    buy_below = to_float(stock.get("buy_below"))
    sell_above = to_float(stock.get("sell_above"))
    stop_loss = to_float(stock.get("stop_loss"))

    if stop_loss is not None and snap.price < stop_loss:
        alerts.append(("price_stop_loss", "\n".join([
            f"🛑 *STOP LOSS ALERT: {ticker}*",
            f"Price dropped to *{fmt_rp(snap.price)}*",
            f"(Stop Loss: < {fmt_rp(stop_loss)})",
            _context_line(snap),
        ])))
    elif buy_below is not None and snap.price < buy_below:
        alerts.append(("price_buy", "\n".join([
            f"🚨 *BUY ALERT: {ticker}*",
            f"Price dropped to *{fmt_rp(snap.price)}*",
            f"(Target: < {fmt_rp(buy_below)})",
            _context_line(snap),
        ])))

    if sell_above is not None and snap.price > sell_above:
        alerts.append(("price_sell", "\n".join([
            f"🎯 *SELL ALERT: {ticker}*",
            f"Price rose to *{fmt_rp(snap.price)}*",
            f"(Target: > {fmt_rp(sell_above)})",
            _context_line(snap),
        ])))

    if snap.rsi is not None and snap.rsi < rsi_threshold:
        lines = [
            f"📉 *TECHNICAL ALERT: {ticker} OVERSOLD*",
            f"RSI is currently at *{snap.rsi:.1f}*.",
            f"Price is {fmt_rp(snap.price)}. Consider looking for entry setups.",
        ]
        if snap.sma:
            gap = (snap.price - snap.sma) / snap.sma * 100
            side = "below" if gap < 0 else "above"
            lines.append(f"SMA(20): {fmt_rp(snap.sma)} (price {abs(gap):.1f}% {side}).")
        alerts.append(("rsi_oversold", "\n".join(lines)))

    return alerts


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #
def _strip_markdown(text: str) -> str:
    return text.replace("*", "").replace("`", "")


def send_telegram(text: str, token: str, chat_id: str, attempts: int = 4) -> None:
    import requests                 # lazy: see module docstring

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            log.warning("Telegram network error (attempt %d): %s",
                        attempt, str(exc).replace(token, "***"))
            time.sleep(2 ** attempt)
            continue

        if resp.ok:
            return

        try:
            body = resp.json()
        except ValueError:
            body = {}
        description = str(body.get("description", resp.text[:200]))

        if resp.status_code == 429:
            wait = int(body.get("parameters", {}).get("retry_after", 5))
            log.warning("Telegram rate limited; retrying in %ss", wait)
            time.sleep(wait)
            continue
        if resp.status_code == 400 and "parse" in description.lower() and "parse_mode" in payload:
            log.warning("Telegram rejected Markdown (%s); resending as plain text.", description)
            payload = {"chat_id": chat_id, "text": _strip_markdown(text),
                       "disable_web_page_preview": True}
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Telegram API error {resp.status_code}: {description}")
    raise RuntimeError("Telegram delivery failed after retries")


# --------------------------------------------------------------------------- #
# Main run
# --------------------------------------------------------------------------- #
def run_sentry(now: datetime, force: bool, dry_run: bool) -> int:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    if not dry_run and not (bot_token and chat_id):
        log.error("BOT_TOKEN and CHAT_ID must be set (or use DRY_RUN=1).")
        return 1

    is_open, reason = market_status(now)
    if not is_open and not force:
        log.info("Market closed (%s) - nothing to do.", reason)
        return 0
    log.info("Running sentry at %s WIB (%s%s)", now.strftime("%Y-%m-%d %H:%M"),
             reason, ", FORCED" if force else "")

    stocks, rsi_threshold = load_watchlist()
    if not stocks:
        log.error("Watchlist is empty or unreadable.")
        return 1

    today = now.date()
    alert_log = AlertLog(ALERT_LOG_PATH, today.isoformat())

    fetch_failures = sent = skipped = delivery_failures = 0
    for stock in stocks:
        symbol = stock["symbol"]
        snap = fetch_snapshot(symbol)
        if snap is None:
            fetch_failures += 1
            continue

        rsi_txt = f"{snap.rsi:.1f}" if snap.rsi is not None else "N/A"
        log.info("%s price=%s RSI=%s SMA20=%s bar=%s", symbol, fmt_rp(snap.price),
                 rsi_txt, fmt_rp(snap.sma), snap.bar_date)

        if snap.bar_date != today and not force:
            log.info("%s: latest bar is %s, not today (holiday / suspension / pre-open delay) - skipping.",
                     symbol, snap.bar_date)
            continue

        for alert_type, message in evaluate(stock, snap, rsi_threshold):
            key = f"{symbol}_{alert_type}"
            if alert_log.has_fired(key):
                log.info("Already alerted today: %s - skipping.", key)
                skipped += 1
                continue

            if dry_run:
                print("\n" + message + "\n")
                sent += 1
                continue

            try:
                send_telegram(message, bot_token, chat_id)
            except Exception as exc:  # noqa: BLE001
                delivery_failures += 1
                log.error("Failed to send %s: %s", key, str(exc).replace(bot_token, "***"))
                continue
            alert_log.record(key)   # record only after successful delivery
            sent += 1
            log.info("Alert sent: %s", key)

    log.info("Done. sent=%d deduplicated=%d fetch_failures=%d delivery_failures=%d",
             sent, skipped, fetch_failures, delivery_failures)

    if delivery_failures or fetch_failures == len(stocks):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IDX intraday price & technical sentry")
    parser.add_argument(
        "--check-market", action="store_true",
        help="Only report whether IDX is in session (writes open/today to $GITHUB_OUTPUT).",
    )
    args = parser.parse_args()

    now = datetime.now(WIB)
    force = env_flag("FORCE_RUN")

    if args.check_market:
        is_open, reason = market_status(now)
        is_open = is_open or force
        log.info("Market gate: open=%s (%s%s)", is_open, reason, ", FORCED" if force else "")
        write_github_output(open=str(is_open).lower(), today=now.date().isoformat())
        return 0

    return run_sentry(now, force=force, dry_run=env_flag("DRY_RUN"))


if __name__ == "__main__":
    sys.exit(main())
