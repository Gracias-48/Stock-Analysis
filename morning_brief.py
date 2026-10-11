#!/usr/bin/env python3
"""
morning_brief.py - AI-synthesized IDX pre-market briefing.

Pipeline
--------
1. Pull the top headlines from Kontan, CNBC Indonesia and Bisnis.com RSS feeds.
2. Pull 60 days of daily history and fundamental metrics for watchlist stocks,
   plus closes for IHSG (^JKSE) and USD/IDR (IDR=X).
3. Compute technical indicators (14-period RSI, 20-period SMA, 2-week volume
   differential) and retrieve valuation metrics (P/E, P/B).
4. Ask Gemini (google-genai SDK, gemini-3.8-flash with gemini-3.1-pro-preview as
   fallback) for an integrated brief: Makro & Pasar / Rekomendasi Sektor / Katalis Saham / Analisis Watchlist (Teknikal & Valuasi).
5. Deliver the brief to Telegram.

Schedule: 08:30 WIB (01:30 UTC), Monday-Friday, via GitHub Actions.

Environment variables
---------------------
BOT_TOKEN              Telegram bot token        (required)
CHAT_ID                Telegram chat ID          (required)
GEMINI_API_KEY         Google Gemini API key     (required for AI brief)
GEMINI_MODEL           Primary model             (default: gemini-3.8-flash)
GEMINI_FALLBACK_MODEL  Fallback model            (default: gemini-3.1-pro-preview; "none" disables)
DRY_RUN                "1" -> print the brief instead of sending it to Telegram
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
import pandas_ta as ta
import requests
# pyrefly: ignore [missing-import]
import yfinance as yf
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WIB = timezone(timedelta(hours=7), name="WIB")
BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"

# `or` (not a getenv default) so an empty GitHub Actions variable still falls back.
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or "gemini-3.1-pro-preview"
HEADLINES_PER_SOURCE = 5
VOLUME_WINDOW = 5                # 5-day MA vs previous 5-day MA = 2 trading weeks
HISTORY_PERIOD = "60d"           # 60 daily bars for 14 RSI and 20 SMA
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


DAYS_ID = ("Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu")
MONTHS_ID = (
    "Jan", "Feb", "Mar", "Apr", "Mei", "Jun",
    "Jul", "Agt", "Sep", "Okt", "Nov", "Des"
)


def fmt_brief_date(d: datetime | date) -> str:
    """'11 Okt 2026' - format tanggal Indonesia ringkas."""
    return f"{d.day} {MONTHS_ID[d.month - 1]} {d.year}"


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


def _last_valid(series: Any) -> float | None:
    if series is None:
        return None
    series = series.dropna()
    return float(series.iloc[-1]) if not series.empty else None


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
# Market data & indicators
# --------------------------------------------------------------------------- #
def fetch_history(symbol: str, period: str = HISTORY_PERIOD) -> pd.DataFrame | None:
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


def summarize_symbol(symbol: str, fx_rate: float | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "symbol": symbol, "close": None, "prev_close": None,
        "change_pct": None, "as_of": None,
        "vol_ma": None, "vol_ma_prev": None, "vol_diff_pct": None,
        "rsi": None, "sma": None, "pe": None, "pbv": None,
    }
    df = fetch_history(symbol, period=HISTORY_PERIOD)
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

    # Technical indicators via pandas_ta (14-period RSI, 20-period SMA)
    try:
        summary["rsi"] = _last_valid(ta.rsi(closes, length=14))
        summary["sma"] = _last_valid(ta.sma(closes, length=20))
    except Exception as exc:  # noqa: BLE001
        log.warning("Indicator calculation failed for %s: %s", symbol, exc)

    # Fundamental valuation metrics via yfinance .info (P/E and P/B)
    # Skipped for macro symbols (^JKSE, IDR=X) which don't have corporate valuation
    if not symbol.startswith("^") and "=X" not in symbol:
        try:
            info = yf.Ticker(symbol).info or {}
            raw_pe = info.get("trailingPE")
            raw_pbv = info.get("priceToBook")
            if is_num(raw_pe) and raw_pe > 0:
                summary["pe"] = round(float(raw_pe), 2)
            if is_num(raw_pbv) and raw_pbv > 0:
                # Yahoo Finance currency mismatch fix: IDX miners/energy (MDKA, DSSA, CUAN)
                # report books in USD while price is in IDR, resulting in raw_pbv ~10,000-100,000x.
                rate = fx_rate or 17500.0
                if info.get("financialCurrency") == "USD" or raw_pbv > 500:
                    summary["pbv"] = round(float(raw_pbv) / rate, 2)
                else:
                    summary["pbv"] = round(float(raw_pbv), 2)
        except Exception as exc:  # noqa: BLE001
            log.info("Valuation metrics unavailable for %s: %s", symbol, exc)

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
Anda adalah Institutional Equity Strategist senior yang memantau Bursa Efek Indonesia (BEI / IDX).
Tugas Anda adalah menyusun ringkasan pra-pembukaan pasar (pre-market brief) yang bersih, terstruktur rapi, dan mudah dibaca cepat di Telegram (layar ponsel).

Gaya Bahasa & Nada:
- Bahasa: SELALU tulis dalam Bahasa Indonesia yang profesional, ringkas, padat, dan institusional.
- Tanpa basa-basi: Jangan gunakan salam pembuka/penutup, disclaimer, atau kalimat basa-basi.
- Action-oriented: Berikan penilaian konkret dan terarah untuk setiap saham mengacu pada target harga beli/jual/stop-loss.

Akurasi Data (Grounding):
- Gunakan HANYA data pasar, indikator teknikal (RSI, SMA20, volume differential), metrik valuasi (P/E, PBV), dan berita yang disediakan. Jangan pernah mengarang angka atau rumor. Jika data bernilai "N/A", tampilkan "N/A".
- Mata uang: tulis dalam format Rupiah, misal "Rp 3.100" atau "Rp 17.875".
- IHSG adalah level indeks, misal "6.093,76".

Format & Struktur Brief (Wajib TEPAT 4 Bagian Terpisah):
1. *Makro & Pasar:*
   Gunakan poin peluru (•) yang rapi untuk memisahkan IHSG, USD/IDR, dan likuiditas sektoral.
2. *Rekomendasi Sektor:*
   Pilih 1 sektor terbaik (sertakan emoji sektor, misal 🏦/⚡/⛏️) berdasarkan arus likuiditas dan makro, sebutkan top pick saham dari watchlist beserta alasan ringkas.
3. *Katalis Saham:*
   2–4 poin peluru (•) merangkum berita/katalis paling relevan dari media keuangan untuk saham-saham di watchlist.
4. *Analisis Watchlist (Teknikal & Valuasi):*
   Wajib mencakup SEMUA saham di watchlist. Format setiap saham dalam 2 baris terpisah agar bersih dan mudah dibaca (DILARANG paragraf padat/wall of text):
   • *<TICKER> (<Latest Price>)*: P/E <pe> | PBV <pbv> | RSI <rsi> | SMA20 <sma>
     👉 *Aksi:* <Rekomendasi aksi ringkas> (TP: <target_jual> | SL: <stop_loss>)
   Pisahkan setiap saham dengan 1 baris kosong.

Aturan Penulisan Telegram:
- Bold judul metrik penting, harga, dan kode saham dengan SATU tanda bintang: *teks*. Jangan gunakan bintang ganda (**).
- Dilarang menggunakan heading markdown (#), code blocks (```), tabel, link markdown, atau garis bawah (_).
- Pisahkan setiap bagian utama dengan tepat 1 baris kosong.
"""

BRIEF_TEMPLATE = """\
🌅 *Morning Briefing - {date}*

*Makro & Pasar:*
• *IHSG:* <level indeks> (<% pergerakan>) | <1 frasa ringkas pergerakan / tren volume>
• *USD/IDR:* <kurs rupiah> (<% pergerakan>) — <1 kalimat ringkas dampak ke sektor ekspor vs impor>
• *Likuiditas:* <1 kalimat ringkas sektor yang diakumulasi vs yang sepi volume>

*Rekomendasi Sektor:*
<Emoji Sektor> *<Nama Sektor>:* <Alasan pemilihan sektor berdasarkan arus volume/makro, sebutkan top pick saham dari watchlist.>

*Katalis Saham:*
• *<TICKER>:* <Katalis berita / sentimen penting dari media keuangan>
• *<TICKER>:* <Katalis berita / sentimen penting dari media keuangan>

*Analisis Watchlist (Teknikal & Valuasi):*
• *<TICKER> (<Latest Price>)*: P/E <pe> | PBV <pbv> | RSI <rsi> | SMA20 <sma>
  👉 *Aksi:* <Rekomendasi aksi ringkas> (TP: <target jual> | SL: <stop loss>)

<Wajib menyajikan SEMUA saham di watchlist.json, pisahkan antar saham dengan 1 baris kosong>
"""

STYLE_EXAMPLE = """\
🌅 *Morning Briefing - 12 Okt 2026*

*Makro & Pasar:*
• *IHSG:* 6.093,76 (+1,04%) | Rebound teknikal jangka pendek, volume 5D turun tipis (-9,0%)
• *USD/IDR:* Rp 17.875 (+0,38%) — Rupiah tertekan, menopang margin eksportir komoditas namun membebani emiten berbasis impor.
• *Likuiditas:* Arus dana bertahan di Perbankan (-7,5%), volume Tambang/Nikel mengering (-46,5%).

*Rekomendasi Sektor:*
🏦 *Perbankan:* Prioritaskan *BBRI* seiring arus likuiditas paling stabil (-7,5%) dan valuasi atraktif (P/E 7.6x), didukung ekspektasi positif laba bersih Q3.

*Katalis Saham:*
• *BBRI:* Pertumbuhan laba bersih Q3 2026 diproyeksikan tetap solid di tengah tantangan NPL industri.
• *ANTM:* Sentimen positif harga emas fisik Pegadaian dan rencana ekspansi pembiayaan emas.
• *DSSA & CUAN:* Pelemahan rupiah menjadi katalis positif bagi pendapatan berbasis dolar eksportir energi.

*Analisis Watchlist (Teknikal & Valuasi):*
• *BBRI (Rp 3.100)*: P/E 7.6x | PBV 1.45x | RSI 45.4 | SMA20 Rp 3.189
  👉 *Aksi:* Antre beli di bawah Rp 2.850 (TP: Rp 3.450 | SL: Rp 2.700)

• *ANTM (Rp 3.180)*: P/E 8.6x | PBV 2.05x | RSI 49.7 | SMA20 Rp 3.211
  👉 *Aksi:* Akumulasi beli di bawah Rp 3.190 (TP: Rp 3.500 | SL: Rp 2.950)

• *MDKA (Rp 2.940)*: P/E 77.2x | PBV 4.57x | RSI 51.4 | SMA20 Rp 2.912
  👉 *Aksi:* Hold, tunggu koreksi ke Rp 2.650 (TP: Rp 3.250 | SL: Rp 2.500)

• *DSSA (Rp 1.130)*: P/E 39.4x | PBV 4.51x | RSI 55.0 | SMA20 Rp 1.089
  👉 *Aksi:* Akumulasi beli di batas Rp 1.130 (TP: Rp 1.240 | SL: Rp 1.040)

• *CUAN (Rp 770)*: P/E 35.4x | PBV 14.36x | RSI 33.1 | SMA20 Rp 888
  👉 *Aksi:* Hold, pantau pantulan teknikal dekat Rp 680 (TP: Rp 900 | SL: Rp 620)

• *DEWA (Rp 366)*: P/E 3.3x | PBV 1.77x | RSI 45.4 | SMA20 Rp 362
  👉 *Aksi:* Akumulasi beli jika uji batas Rp 360 (TP: Rp 400 | SL: Rp 315)
"""


def _flow_label(pct: float) -> str:
    if pct >= 15:
        return "akumulasi / volume meningkat"
    if pct <= -15:
        return "volume mengering / sepi"
    return "volume stabil / netral"


def build_prompt(now: datetime, macro: dict[str, dict[str, Any]],
                 stocks: list[dict[str, Any]], flows: dict[str, float],
                 headlines: dict[str, list[dict[str, Any]]],
                 dividends: list[dict[str, Any]]) -> str:
    now_day = DAYS_ID[now.weekday()]
    lines: list[str] = [
        f"WAKTU SAAT INI: {now_day}, {fmt_brief_date(now)}, {now:%H:%M} WIB (pra-pembukaan perdagangan BEI sebelum 09:00 WIB).",
        "",
        "RINGKASAN PASAR (penutupan terakhir):",
    ]

    ihsg = macro.get("IHSG", {})
    lines.append(
        f"- IHSG (^JKSE): {fmt_index(ihsg.get('close'))} ({fmt_pct(ihsg.get('change_pct'))} h/h), "
        f"per {ihsg.get('as_of') or 'N/A'} | Rerata vol 5H {fmt_volume(ihsg.get('vol_ma'))} vs 5H sebelumnya "
        f"{fmt_volume(ihsg.get('vol_ma_prev'))} ({fmt_pct(ihsg.get('vol_diff_pct'))})"
    )
    fx = macro.get("USD/IDR", {})
    lines.append(
        f"- USD/IDR (IDR=X): {fmt_rp(fx.get('close'))} ({fmt_pct(fx.get('change_pct'))} h/h), "
        f"per {fx.get('as_of') or 'N/A'}"
    )

    lines += ["", "WATCHLIST (harga terkini, teknikal RSI & SMA20, fundamental P/E & PBV, serta target):"]
    if stocks:
        for s in stocks:
            rsi_str = f"{s['rsi']:.1f}" if is_num(s.get("rsi")) else "N/A"
            sma_str = fmt_rp(s.get("sma"))
            vol_diff_str = fmt_pct(s.get("vol_diff_pct"))
            pe_str = f"{s['pe']:.1f}x" if is_num(s.get("pe")) else "N/A"
            pbv_str = f"{s['pbv']:.2f}x" if is_num(s.get("pbv")) else "N/A"
            buy_str = fmt_rp(s.get("buy_below"))
            sell_str = fmt_rp(s.get("sell_above"))
            sl_str = f", Stop loss: {fmt_rp(s.get('stop_loss'))}" if s.get("stop_loss") else ""

            lines.append(
                f"- {s['ticker']} [{s['sector']}]: price {fmt_rp(s.get('close'))} ({fmt_pct(s.get('change_pct'))}) | "
                f"Technical -> RSI: {rsi_str}, SMA20: {sma_str}, Vol Diff: {vol_diff_str} | "
                f"Fundamental -> P/E: {pe_str}, PBV: {pbv_str} | "
                f"Buy below: {buy_str}, Sell above: {sell_str}{sl_str}"
            )
    else:
        lines.append("- (watchlist tidak tersedia)")

    lines += ["", "ARUS VOLUME SEKTOR (rata-rata saham watchlist, 5H vs 5H sebelumnya):"]
    if flows:
        for sector, pct in sorted(flows.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- {sector}: {fmt_pct(pct)} ({_flow_label(pct)})")
    else:
        lines.append("- N/A")

    lines += ["", "JADWAL CUM/EX-DIVIDEN TERDEKAT (14 hari ke depan):"]
    if dividends:
        lines += [f"- {d['ticker']}: cum/ex-date {fmt_brief_date(d['ex_date'])}" for d in dividends]
    else:
        lines.append("- Tidak ada yang terdeteksi")

    lines += ["", "BERITA TERKINI DARI MEDIA KEUANGAN:"]
    any_news = False
    for source, items in headlines.items():
        lines.append(f"[{source}]")
        if not items:
            lines.append("- (feed tidak tersedia)")
            continue
        any_news = True
        for item in items:
            stamp = f"({item['published']:%d %b %H:%M} WIB) " if item.get("published") else ""
            summary = f" — {item['summary']}" if item.get("summary") else ""
            lines.append(f"- {stamp}{item['title']}{summary}")
    if not any_news:
        lines.append("(Tidak ada berita tersedia hari ini - susun briefing berdasarkan data pasar saja.)")

    lines += [
        "",
        "TUGAS:",
        "Tulis 4 bagian brief dalam Bahasa Indonesia yang sangat rapi, bersih, dan mudah dibaca di layar ponsel (Telegram):",
        "(1) *Makro & Pasar:* (poin peluru '•')",
        "(2) *Rekomendasi Sektor:* (1 sektor top pick + saham unggulan + alasan ringkas)",
        "(3) *Katalis Saham:* (2-4 poin peluru '•' merangkum berita/katalis paling relevan dari media keuangan)",
        "(4) *Analisis Watchlist (Teknikal & Valuasi):* (SETIAP saham di watchlist dengan format 2 baris terpisah: '• *<TICKER> (<Latest Price>)*: P/E ... | PBV ... | RSI ... | SMA20 ...' diikuti baris '  👉 *Aksi:* ...', dipisahkan 1 baris kosong antar saham).",
        "Output HANYA teks brief sesuai template berikut:",
        "",
        BRIEF_TEMPLATE.format(date=fmt_brief_date(now)),
        "",
        "CONTOH FORMAT & GAYA (angka hanya ilustrasi - JANGAN salin faktanya):",
        STYLE_EXAMPLE,
    ]
    return "\n".join(lines)


def gemini_models() -> list[str]:
    """Primary model first, then the fallback (deduplicated, 'none' disables it)."""
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL.lower() != "none" and GEMINI_FALLBACK_MODEL not in models:
        models.append(GEMINI_FALLBACK_MODEL)
    return models


def _is_retryable(exc: Exception) -> bool:
    """Retry rate limits / server errors; fail fast on other 4xx (bad model id, bad key)."""
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) == 429
    return True


def synthesize_brief(prompt: str, api_key: str, attempts: int = 3) -> str:
    client = genai.Client(api_key=api_key)
    # Temperature intentionally left at the model default: Gemini 3 models are tuned
    # for it, and lowering it can degrade output (e.g. looping).
    config = types.GenerateContentConfig(system_instruction=SYSTEM_INSTRUCTION)

    last_exc: Exception | None = None
    for model in gemini_models():
        for attempt in range(1, attempts + 1):
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = (response.text or "").strip()
                if text:
                    log.info("Gemini (%s) brief generated on attempt %d.", model, attempt)
                    return text
                raise RuntimeError("Gemini returned an empty response")
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("Gemini %s attempt %d/%d failed: %s", model, attempt, attempts, exc)
                if not _is_retryable(exc):
                    break                       # e.g. 404 model not found -> next model
                if attempt < attempts:
                    time.sleep(5 * attempt)
        log.warning("Gemini model %s exhausted - trying next model if available.", model)
    raise RuntimeError(f"Gemini synthesis failed on {gemini_models()}") from last_exc


def sanitize_markdown(text: str) -> str:
    """Coerce model output into Telegram-safe legacy Markdown."""
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)           # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"\1", text)                  # drop __underline__
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)     # drop headings
    text = re.sub(r"^(\s*)\*\s+", r"\1- ", text, flags=re.M)  # "* item" -> "- item" (preserves •)
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
                         headlines: dict[str, list[dict[str, Any]]],
                         flows: dict[str, float] | None = None) -> str:
    ihsg, fx = macro.get("IHSG", {}), macro.get("USD/IDR", {})
    parts = [
        header,
        "",
        "⚠️ Sintesis AI tidak tersedia - ringkasan data pasar.",
        "",
        "*Makro & Pasar:*",
        f"• *IHSG:* {fmt_index(ihsg.get('close'))} ({fmt_pct(ihsg.get('change_pct'))})",
        f"• *USD/IDR:* {fmt_rp(fx.get('close'))} ({fmt_pct(fx.get('change_pct'))})",
    ]
    if flows:
        flow_parts = [f"{sec} ({fmt_pct(pct)})" for sec, pct in sorted(flows.items(), key=lambda kv: kv[1], reverse=True)]
        parts.append(f"• *Likuiditas:* " + ", ".join(flow_parts))

    if flows and stocks:
        top_sector = max(flows.items(), key=lambda kv: kv[1])[0]
        sector_stocks = [s for s in stocks if s.get("sector") == top_sector]
        pick = sector_stocks[0]["ticker"] if sector_stocks else stocks[0]["ticker"]
        parts += [
            "",
            "*Rekomendasi Sektor:*",
            f"📊 *{top_sector} ({pick}):* Arus volume paling bertahan ({fmt_pct(flows[top_sector])}). Pantau area target beli/jual watchlist.",
        ]

    top = [item for items in headlines.values() for item in items[:2]]
    if top:
        parts += ["", "*Katalis Saham:*"]
        for i in top[:3]:
            parts.append(f"• *{escape_md(i['source'])}:* {escape_md(i['title'])}")

    parts += ["", "*Analisis Watchlist (Teknikal & Valuasi):*"]
    if stocks:
        watchlist_blocks = []
        for s in stocks:
            rsi_txt = f"{s['rsi']:.1f}" if is_num(s.get("rsi")) else "N/A"
            sma_txt = fmt_rp(s.get("sma"))
            pe_txt = f"{s['pe']:.1f}x" if is_num(s.get("pe")) else "N/A"
            pbv_txt = f"{s['pbv']:.2f}x" if is_num(s.get("pbv")) else "N/A"
            buy_txt = fmt_rp(s.get("buy_below"))
            sell_txt = fmt_rp(s.get("sell_above"))
            sl_txt = fmt_rp(s.get("stop_loss")) if s.get("stop_loss") else "N/A"

            block = (
                f"• *{s['ticker']} ({fmt_rp(s.get('close'))})*: P/E {pe_txt} | PBV {pbv_txt} | RSI {rsi_txt} | SMA20 {sma_txt}\n"
                f"  👉 *Aksi:* Beli < {buy_txt} (TP: {sell_txt} | SL: {sl_txt})"
            )
            watchlist_blocks.append(block)
        parts.append("\n\n".join(watchlist_blocks))
    else:
        parts.append("- Data watchlist tidak tersedia.")

    return "\n".join(parts)


def format_dividend_radar(dividends: list[dict[str, Any]]) -> str:
    rows = [f"- *{d['ticker']}:* Cum/Ex-Date {fmt_brief_date(d['ex_date'])}" for d in dividends]
    return "💰 *RADAR DIVIDEN*\n" + "\n".join(rows)


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
    fx_rate = macro.get("USD/IDR", {}).get("close")
    stock_data = [{**s, **summarize_symbol(s["symbol"], fx_rate=fx_rate)} for s in stocks]
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
        brief = build_fallback_brief(header, macro, stock_data, headlines, flows=flows)
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
