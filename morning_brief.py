#!/usr/bin/env python3
"""
morning_brief.py - AI-synthesized IDX pre-market briefing.

Pipeline
--------
1. Pull the top headlines from Kontan, CNBC Indonesia and Bisnis.com RSS feeds.
2. Pull last closes for IHSG (^JKSE), USD/IDR (IDR=X) and every watchlist stock.
3. Compute a 2-week volume differential per stock
   (current 5-day average volume vs. the previous 5-day average volume).
4. Ask Gemini (google-genai SDK, gemini-2.5-flash) for a 3-part brief:
   Macro & Momentum / Recommendation / Key Catalysts.
5. Deliver the brief to Telegram.

Schedule: 08:30 WIB (01:30 UTC), Monday-Friday, via GitHub Actions.

Environment variables
---------------------
BOT_TOKEN       Telegram bot token                       (required)
CHAT_ID         Telegram chat ID                         (required)
GEMINI_API_KEY  Google Gemini API key                    (required for AI brief)
GEMINI_MODEL    Override model name (default: gemini-2.5-flash)
DRY_RUN         "1" -> print the brief instead of sending it to Telegram
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# pyrefly: ignore [missing-import]
import feedparser
import pandas as pd
import requests
# pyrefly: ignore [missing-import]
import yfinance as yf
from google import genai
from google.genai import types

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WIB = timezone(timedelta(hours=7), name="WIB")
BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
HEADLINES_PER_SOURCE = 5
VOLUME_WINDOW = 5                # 5-day MA vs previous 5-day MA = 2 trading weeks
DIVIDEND_HORIZON_DAYS = 14
HTTP_TIMEOUT = 20
TELEGRAM_MAX_LEN = 4000          # Telegram hard limit is 4096 chars
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# RSS sources, tried in order until one returns entries (verified Oct 2026).
# - Kontan: subdomain feeds are live; the legacy rss.kontan.co.id host fails TLS.
# - CNBC Indonesia: official section feeds.
# - Bisnis.com: every bisnis.com endpoint (incl. homepage) returns HTTP 403 to
#   non-browser clients (bot protection), so it is sourced through Google News
#   RSS scoped to the bisnis.com domain.
NEWS_SOURCES: dict[str, list[str]] = {
    "Kontan": [
        "https://investasi.kontan.co.id/rss",
        "https://keuangan.kontan.co.id/rss",
    ],
    "CNBC Indonesia": [
        "https://www.cnbcindonesia.com/market/rss",
        "https://www.cnbcindonesia.com/news/rss",
    ],
    "Bisnis.com": [
        "https://news.google.com/rss/search?q=site:market.bisnis.com+when:2d&hl=id&gl=ID&ceid=ID:id",
        "https://news.google.com/rss/search?q=site:bisnis.com+saham+when:2d&hl=id&gl=ID&ceid=ID:id",
    ],
}

MACRO_SYMBOLS = {"IHSG": "^JKSE", "USD/IDR": "IDR=X"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("morning_brief")


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def fmt_rp(value: float | None) -> str:
    return f"Rp {value:,.0f}" if is_num(value) else "N/A"


def fmt_index(value: float | None) -> str:
    return f"{value:,.2f}" if is_num(value) else "N/A"


def fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if is_num(value) else "N/A"


def fmt_volume(value: float | None) -> str:
    if not is_num(value):
        return "N/A"
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:,.1f}{suffix}"
    return f"{value:,.0f}"


def fmt_brief_date(d: datetime | date) -> str:
    """'Oct 12, 2026' - locale independent."""
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{months[d.month - 1]} {d.day}, {d.year}"


def escape_md(text: str) -> str:
    """Escape Telegram legacy-Markdown control characters in untrusted text."""
    return re.sub(r"([_*`\[])", r"\\\1", text)


def clean_text(raw: str | None, limit: int = 220) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


# --------------------------------------------------------------------------- #
# Watchlist
# --------------------------------------------------------------------------- #
def to_yahoo_symbol(ticker: str) -> str:
    ticker = ticker.strip().upper()
    return ticker if ticker.endswith(".JK") else f"{ticker}.JK"


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Cannot read watchlist %s: %s", path, exc)
        return []

    raw_stocks = data.get("stocks", []) if isinstance(data, dict) else data
    stocks: list[dict[str, Any]] = []
    for item in raw_stocks or []:
        if not isinstance(item, dict) or not item.get("ticker"):
            log.warning("Skipping malformed watchlist entry: %r", item)
            continue
        ticker = str(item["ticker"]).strip().upper().removesuffix(".JK")
        stocks.append({
            **item,
            "ticker": ticker,
            "symbol": to_yahoo_symbol(ticker),
            "sector": item.get("sector") or "Unclassified",
        })
    return stocks


# --------------------------------------------------------------------------- #
# News aggregation
# --------------------------------------------------------------------------- #
def _entry_time_wib(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc).astimezone(WIB)
    except (TypeError, ValueError):
        return None


def fetch_feed_headlines(source: str, urls: list[str],
                         limit: int = HEADLINES_PER_SOURCE) -> list[dict[str, Any]]:
    """Return the latest `limit` headlines from the first working URL."""
    for url in urls:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
                },
                timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                raise ValueError(f"no entries (bozo={feed.get('bozo_exception')!r})")

            is_aggregator = "news.google.com" in url
            entries = sorted(
                feed.entries,
                key=lambda e: _entry_time_wib(e) or datetime.min.replace(tzinfo=WIB),
                reverse=True,
            )

            items: list[dict[str, Any]] = []
            seen: set[str] = set()
            for entry in entries:
                title = clean_text(entry.get("title"), 200)
                if is_aggregator:
                    # Google News appends " - Bisnis.com - Market" etc.
                    title = re.split(r"\s+-\s+Bisnis", title)[0].strip()
                if not title or title.lower() in seen:
                    continue
                seen.add(title.lower())
                items.append({
                    "source": source,
                    "title": title,
                    "summary": "" if is_aggregator else clean_text(entry.get("summary")),
                    "published": _entry_time_wib(entry),
                })
                if len(items) >= limit:
                    break

            if items:
                log.info("[%s] %d headlines from %s", source, len(items), url)
                return items
            log.warning("[%s] feed parsed but yielded no usable items: %s", source, url)
        except Exception as exc:  # noqa: BLE001 - one bad feed must never kill the run
            log.warning("[%s] feed failed (%s): %s", source, url, exc)

    log.error("[%s] all feed URLs failed - continuing without this source.", source)
    return []


def gather_headlines() -> dict[str, list[dict[str, Any]]]:
    return {src: fetch_feed_headlines(src, urls) for src, urls in NEWS_SOURCES.items()}


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
def fetch_history(symbol: str, period: str = "1mo") -> pd.DataFrame | None:
    try:
        df = yf.Ticker(symbol).history(
            period=period, interval="1d", auto_adjust=False, actions=False
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("yfinance history failed for %s: %s", symbol, exc)
        return None

    if df is None or df.empty or "Close" not in df.columns:
        log.warning("No price history returned for %s", symbol)
        return None

    df = df.dropna(subset=["Close"])
    # Drop zero-volume placeholder bars (e.g. pre-open rows) for traded instruments.
    # FX pairs report zero volume everywhere, so leave those untouched.
    if "Volume" in df.columns and (df["Volume"] > 0).any():
        df = df[df["Volume"] > 0]
    return df if not df.empty else None


def volume_differential(df: pd.DataFrame, window: int = VOLUME_WINDOW) -> dict[str, float | None]:
    """Current N-day average volume vs. the preceding N-day average volume."""
    result: dict[str, float | None] = {"vol_ma": None, "vol_ma_prev": None, "vol_diff_pct": None}
    if "Volume" not in df.columns:
        return result

    volume = df["Volume"].dropna().astype(float)
    if len(volume) < 2 * window or not (volume > 0).any():
        return result

    rolling = volume.rolling(window).mean()
    current = float(rolling.iloc[-1])               # mean of last 5 sessions
    previous = float(rolling.iloc[-1 - window])     # mean of the 5 sessions before that
    result["vol_ma"] = current
    result["vol_ma_prev"] = previous
    if previous > 0:
        result["vol_diff_pct"] = (current - previous) / previous * 100
    return result


def summarize_symbol(symbol: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "symbol": symbol, "close": None, "prev_close": None,
        "change_pct": None, "as_of": None,
        "vol_ma": None, "vol_ma_prev": None, "vol_diff_pct": None,
    }
    df = fetch_history(symbol)
    if df is None:
        return summary

    closes = df["Close"].astype(float)
    summary["close"] = float(closes.iloc[-1])
    summary["as_of"] = df.index[-1].strftime("%Y-%m-%d")
    if len(closes) >= 2:
        prev = float(closes.iloc[-2])
        summary["prev_close"] = prev
        if prev:
            summary["change_pct"] = (summary["close"] - prev) / prev * 100
    summary.update(volume_differential(df))
    return summary


def sector_volume_flows(stocks: list[dict[str, Any]]) -> dict[str, float]:
    buckets: dict[str, list[float]] = {}
    for s in stocks:
        if is_num(s.get("vol_diff_pct")):
            buckets.setdefault(s["sector"], []).append(s["vol_diff_pct"])
    return {sector: sum(v) / len(v) for sector, v in buckets.items() if v}


def _coerce_date(value: Any) -> date | None:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        ts = pd.Timestamp(value)
        return None if pd.isna(ts) else ts.date()
    except (ValueError, TypeError):
        return None


def fetch_dividend_radar(stocks: list[dict[str, Any]], today: date,
                         horizon_days: int = DIVIDEND_HORIZON_DAYS) -> list[dict[str, Any]]:
    """Watchlist stocks with an ex-dividend date in the next `horizon_days`."""
    radar: list[dict[str, Any]] = []
    for stock in stocks:
        try:
            calendar = yf.Ticker(stock["symbol"]).calendar
        except Exception as exc:  # noqa: BLE001
            log.info("No calendar for %s: %s", stock["symbol"], exc)
            continue

        ex_raw = None
        if isinstance(calendar, dict):
            ex_raw = calendar.get("Ex-Dividend Date")
        elif isinstance(calendar, pd.DataFrame) and "Ex-Dividend Date" in calendar.index:
            ex_raw = calendar.loc["Ex-Dividend Date"].iloc[0]

        ex_date = _coerce_date(ex_raw)
        if ex_date and today <= ex_date <= today + timedelta(days=horizon_days):
            radar.append({"ticker": stock["ticker"], "ex_date": ex_date})
    return radar


# --------------------------------------------------------------------------- #
# Gemini synthesis
# --------------------------------------------------------------------------- #
SYSTEM_INSTRUCTION = """\
You are a senior institutional equity strategist covering the Indonesia Stock Exchange (IDX).
You write a pre-market brief that is read on a phone lock screen via Telegram.

Tone and voice:
- Objective, institutional, concise. No greetings, sign-offs, disclaimers or conversational filler.
- Action-oriented: name exact tickers, price levels and the direction of conviction.

Grounding:
- Use ONLY the market data and headlines supplied by the user. Never invent prices, numbers,
  events or corporate actions. If a data point is "N/A", do not guess it.
- Write in English, even though some headlines are in Bahasa Indonesia.
- Currency must be written as Indonesian Rupiah with thousand separators, e.g. "Rp 4,500".
  IHSG is an index level, not Rupiah (e.g. "7,200").

Formatting (Telegram legacy Markdown - strict):
- Bold with SINGLE asterisks only: *text*. Never use double asterisks.
- No '#' headings, no code blocks or backticks, no tables, no links, no underscores for emphasis.
- Bullets start with "- ".
- Separate sections with exactly one blank line.
"""

BRIEF_TEMPLATE = """\
🌅 *Morning Briefing - {date}*

*Macro & Momentum:*
<2-4 sentences: IHSG level and daily move; USD/IDR level and which sectors it helps or hurts;
volume-flow read by sector (where accumulation is building vs. where volume is drying up).>

*Recommendation:*
<sector emoji> *<Sector>.* <ONE explicit sector call. Name the 1-2 watchlist tickers to prioritize
and the data-backed reason (price vs. buy/sell levels, volume trend, news).>

*Key Catalysts:*
- *TICKER:* <one-line catalyst drawn from the headlines or the price/volume data>
<one bullet per watchlist ticker that has a relevant catalyst; maximum 6 bullets>
"""

STYLE_EXAMPLE = """\
🌅 *Morning Briefing - Oct 12, 2026*

*Macro & Momentum:*
IHSG is at 7,200. USD/IDR is hovering at Rp 15,500, creating tailwinds for export miners. Volume analysis shows strong institutional accumulation in banking, while coal volumes are drying up.

*Recommendation:*
🏦 *Banking.* Prioritize accumulating BBRI. Foreign flow is stabilizing and the P/E ratio is historically cheap ahead of the Q3 earnings cycle.

*Key Catalysts:*
- *ANTM:* Nickel prices dropped 2% overnight in London.
- *DSSA:* Company announced potential expansion into renewable assets.
"""


def _flow_label(pct: float) -> str:
    if pct >= 15:
        return "rising participation / accumulation"
    if pct <= -15:
        return "volume drying up"
    return "flat"


def build_prompt(now: datetime, macro: dict[str, dict[str, Any]],
                 stocks: list[dict[str, Any]], flows: dict[str, float],
                 headlines: dict[str, list[dict[str, Any]]],
                 dividends: list[dict[str, Any]]) -> str:
    lines: list[str] = [
        f"CURRENT TIME: {now:%A} {fmt_brief_date(now)}, {now:%H:%M} WIB (pre-market, before the 09:00 WIB open).",
        "",
        "MARKET SNAPSHOT (last available close):",
    ]

    ihsg = macro.get("IHSG", {})
    lines.append(
        f"- IHSG (^JKSE): {fmt_index(ihsg.get('close'))} ({fmt_pct(ihsg.get('change_pct'))} d/d), "
        f"as of {ihsg.get('as_of') or 'N/A'} | 5D avg volume {fmt_volume(ihsg.get('vol_ma'))} vs prior 5D "
        f"{fmt_volume(ihsg.get('vol_ma_prev'))} ({fmt_pct(ihsg.get('vol_diff_pct'))})"
    )
    fx = macro.get("USD/IDR", {})
    lines.append(
        f"- USD/IDR (IDR=X): {fmt_rp(fx.get('close'))} ({fmt_pct(fx.get('change_pct'))} d/d), "
        f"as of {fx.get('as_of') or 'N/A'}"
    )

    lines += ["", "WATCHLIST (2-week volume differential = current 5D avg volume vs previous 5D avg volume):"]
    if stocks:
        for s in stocks:
            lines.append(
                f"- {s['ticker']} [{s['sector']}] {s.get('name', '')}: close {fmt_rp(s.get('close'))} "
                f"({fmt_pct(s.get('change_pct'))} d/d) | buy-below {fmt_rp(s.get('buy_below'))} / "
                f"sell-above {fmt_rp(s.get('sell_above'))} | 5D avg vol {fmt_volume(s.get('vol_ma'))} vs prior "
                f"{fmt_volume(s.get('vol_ma_prev'))} ({fmt_pct(s.get('vol_diff_pct'))})"
            )
    else:
        lines.append("- (watchlist unavailable)")

    lines += ["", "SECTOR VOLUME FLOW (average of watchlist names, 5D vs prior 5D):"]
    if flows:
        for sector, pct in sorted(flows.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- {sector}: {fmt_pct(pct)} ({_flow_label(pct)})")
    else:
        lines.append("- N/A")

    lines += ["", "UPCOMING EX-DIVIDEND DATES (next 14 days):"]
    if dividends:
        lines += [f"- {d['ticker']}: ex-date {fmt_brief_date(d['ex_date'])}" for d in dividends]
    else:
        lines.append("- None detected")

    lines += ["", "LATEST HEADLINES:"]
    any_news = False
    for source, items in headlines.items():
        lines.append(f"[{source}]")
        if not items:
            lines.append("- (feed unavailable)")
            continue
        any_news = True
        for item in items:
            stamp = f"({item['published']:%b %d %H:%M} WIB) " if item.get("published") else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(f"- {stamp}{item['title']}{summary}")
    if not any_news:
        lines.append("(No headlines available today - base the brief on market data only.)")

    lines += [
        "",
        "TASK:",
        "Write the 3-part brief: (1) Macro & Momentum, (2) one Explicit Sector Recommendation, "
        "(3) Key Catalysts for the watchlist stocks. Output ONLY the brief, using EXACTLY this structure:",
        "",
        BRIEF_TEMPLATE.format(date=fmt_brief_date(now)),
        "Suggested sector emojis: 🏦 Banking, ⛏️ Metals & Mining, ⚡ Energy/Coal, 🏗️ Infrastructure, "
        "🛒 Consumer, 📡 Telco/Tech, 🏠 Property.",
        "",
        "STYLE REFERENCE (illustrative numbers only - do NOT copy its facts):",
        STYLE_EXAMPLE,
    ]
    return "\n".join(lines)


def synthesize_brief(prompt: str, api_key: str, attempts: int = 3) -> str:
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.4,
    )
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt, config=config
            )
            text = (response.text or "").strip()
            if text:
                log.info("Gemini (%s) brief generated on attempt %d.", GEMINI_MODEL, attempt)
                return text
            raise RuntimeError("Gemini returned an empty response")
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log.warning("Gemini attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Gemini synthesis failed after {attempts} attempts") from last_exc


def sanitize_markdown(text: str) -> str:
    """Coerce model output into Telegram-safe legacy Markdown."""
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)           # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"\1", text)                  # drop __underline__
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)     # drop headings
    text = re.sub(r"^(\s*)[•*]\s+", r"\1- ", text, flags=re.M)  # "* item" -> "- item"
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def enforce_brief_format(text: str, header: str) -> str:
    text = sanitize_markdown(text)
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("🌅"):
        lines[0] = header                                     # canonical header/date
        return "\n".join(lines)
    return f"{header}\n\n{text}"


# --------------------------------------------------------------------------- #
# Deterministic sections (fallback + dividend radar)
# --------------------------------------------------------------------------- #
def build_fallback_brief(header: str, macro: dict[str, dict[str, Any]],
                         stocks: list[dict[str, Any]],
                         headlines: dict[str, list[dict[str, Any]]]) -> str:
    ihsg, fx = macro.get("IHSG", {}), macro.get("USD/IDR", {})
    parts = [
        header,
        "",
        "⚠️ AI synthesis unavailable - raw data snapshot.",
        "",
        "*Macro & Momentum:*",
        f"IHSG *{fmt_index(ihsg.get('close'))}* ({fmt_pct(ihsg.get('change_pct'))}). "
        f"USD/IDR *{fmt_rp(fx.get('close'))}* ({fmt_pct(fx.get('change_pct'))}).",
        "",
        "*Watchlist:*",
    ]
    if stocks:
        for s in stocks:
            parts.append(
                f"- *{s['ticker']}:* {fmt_rp(s.get('close'))} ({fmt_pct(s.get('change_pct'))}) "
                f"| Vol 2W {fmt_pct(s.get('vol_diff_pct'))}"
            )
    else:
        parts.append("- Watchlist data unavailable.")

    top = [item for items in headlines.values() for item in items[:2]]
    if top:
        parts += ["", "*Top Headlines:*"]
        parts += [f"- {escape_md(i['source'])}: {escape_md(i['title'])}" for i in top]
    return "\n".join(parts)


def format_dividend_radar(dividends: list[dict[str, Any]]) -> str:
    rows = [f"- *{d['ticker']}:* Ex-date {fmt_brief_date(d['ex_date'])}" for d in dividends]
    return "💰 *DIVIDEND RADAR*\n" + "\n".join(rows)


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #
def strip_markdown(text: str) -> str:
    text = re.sub(r"\\([_*`\[])", r"\1", text)
    return re.sub(r"[*`]", "", text)


def split_message(text: str, limit: int = TELEGRAM_MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current.rstrip())
            current = ""
        while len(line) > limit:                    # pathological single long line
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current.strip():
        chunks.append(current.rstrip())
    return chunks


def _send_chunk(text: str, token: str, chat_id: str, attempts: int = 4) -> None:
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
            payload = {"chat_id": chat_id, "text": strip_markdown(text),
                       "disable_web_page_preview": True}
            continue
        if resp.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        raise RuntimeError(f"Telegram API error {resp.status_code}: {description}")
    raise RuntimeError("Telegram delivery failed after retries")


def send_telegram(text: str, token: str, chat_id: str) -> None:
    for chunk in split_message(text):
        _send_chunk(chunk, token, chat_id)
    log.info("Brief delivered to Telegram.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main() -> int:
    now = datetime.now(WIB)
    dry_run = os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()

    if not dry_run and not (bot_token and chat_id):
        log.error("BOT_TOKEN and CHAT_ID must be set (or use DRY_RUN=1).")
        return 1

    log.info("Building morning brief for %s WIB", now.strftime("%Y-%m-%d %H:%M"))

    stocks = load_watchlist()
    if not stocks:
        log.warning("Watchlist is empty or unreadable - brief will cover macro only.")

    headlines = gather_headlines()
    macro = {label: summarize_symbol(sym) for label, sym in MACRO_SYMBOLS.items()}
    stock_data = [{**s, **summarize_symbol(s["symbol"])} for s in stocks]
    flows = sector_volume_flows(stock_data)
    dividends = fetch_dividend_radar(stocks, now.date())

    header = f"🌅 *Morning Briefing - {fmt_brief_date(now)}*"
    brief: str | None = None
    if gemini_key:
        try:
            prompt = build_prompt(now, macro, stock_data, flows, headlines, dividends)
            brief = enforce_brief_format(synthesize_brief(prompt, gemini_key), header)
        except Exception as exc:  # noqa: BLE001
            log.error("AI synthesis failed, falling back to data snapshot: %s", exc)
    else:
        log.error("GEMINI_API_KEY is not set - sending data-only fallback brief.")

    if not brief:
        brief = build_fallback_brief(header, macro, stock_data, headlines)
    if dividends:
        brief = f"{brief}\n\n{format_dividend_radar(dividends)}"

    if dry_run:
        print("\n" + "=" * 60 + "\n" + brief + "\n" + "=" * 60)
        return 0

    try:
        send_telegram(brief, bot_token, chat_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to deliver brief: %s", str(exc).replace(bot_token, "***"))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
