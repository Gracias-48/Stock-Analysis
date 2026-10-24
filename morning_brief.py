#!/usr/bin/env python3
"""
morning_brief.py - AI-synthesized IDX pre-market macro briefing & catalyst radar.

Pipeline
--------
1. Pull top headlines from Kontan, CNBC Indonesia, and Bisnis.com RSS feeds.
2. Pull market data for IHSG (^JKSE), USD/IDR (IDR=X), Gold (GC=F), and Crude Oil (CL=F).
3. Compute sector liquidity flows across key IDX sector leader proxies.
4. Synthesize via Gemini (gemini-3.8-flash, fallback gemini-3.1-pro-preview / gemini-2.5-flash):
   - Kondisi Makro & Nilai Tukar (IHSG, USD/IDR, Global Commodities)
   - Radar Berita & Katalis Utama Hari Ini (sharp news synthesis)
   - Rotasi & Rekomendasi Sektor Unggulan (with /analyze <TICKER> call-to-action)
5. Deliver the executive brief to Telegram.

Schedule: 08:25 WIB (01:25 UTC), Monday-Friday, via GitHub Actions.
"""

from __future__ import annotations

import html
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
from google.genai import errors as genai_errors
from google.genai import types

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
WIB = timezone(timedelta(hours=7), name="WIB")
BASE_DIR = Path(__file__).resolve().parent

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or "gemini-3.1-pro-preview"
HEADLINES_PER_SOURCE = 5
VOLUME_WINDOW = 5
HISTORY_PERIOD = "60d"
HTTP_TIMEOUT = 20
TELEGRAM_MAX_LEN = 4000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

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

MACRO_SYMBOLS: dict[str, str] = {
    "IHSG": "^JKSE",
    "USD/IDR": "IDR=X",
    "Gold (Emas)": "GC=F",
    "Crude Oil (WTI)": "CL=F",
}

# Key sector leader proxies to calculate aggregate IDX sector volume flows
SECTOR_LEADER_PROXIES: dict[str, list[str]] = {
    "Perbankan": ["BBCA.JK", "BBRI.JK", "BMRI.JK"],
    "Energi & Batubara": ["ADRO.JK", "PTBA.JK", "MEDC.JK"],
    "Tambang & Mineral": ["ANTM.JK", "MDKA.JK", "INCO.JK"],
    "Telekomunikasi": ["TLKM.JK", "ISAT.JK"],
    "Konsumer & Konglomerasi": ["ICBP.JK", "ASII.JK"],
    "Infrastruktur & Gas": ["PGAS.JK", "JSMR.JK"],
}

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


def fmt_usd(value: float | None) -> str:
    return f"${value:,.2f}" if is_num(value) else "N/A"


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
    """'12 Okt 2026' - format tanggal Indonesia ringkas."""
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
        except Exception as exc:  # noqa: BLE001
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
    if "Volume" in df.columns and (df["Volume"] > 0).any():
        df = df[df["Volume"] > 0]
    return df if not df.empty else None


def volume_differential(df: pd.DataFrame, window: int = VOLUME_WINDOW) -> dict[str, float | None]:
    result: dict[str, float | None] = {"vol_ma": None, "vol_ma_prev": None, "vol_diff_pct": None}
    if "Volume" not in df.columns:
        return result

    volume = df["Volume"].dropna().astype(float)
    if len(volume) < 2 * window or not (volume > 0).any():
        return result

    rolling = volume.rolling(window).mean()
    current = float(rolling.iloc[-1])
    previous = float(rolling.iloc[-1 - window])
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
    return summary


def calculate_sector_liquidity_flows(proxies: dict[str, list[str]] = SECTOR_LEADER_PROXIES) -> dict[str, float]:
    """Calculate 5-day volume momentum vs previous volume across sector leader proxies."""
    flows: dict[str, float] = {}
    for sector, tickers in proxies.items():
        diffs: list[float] = []
        for ticker in tickers:
            df = fetch_history(ticker, period="30d")
            if df is not None and len(df) >= 10:
                v_diff = volume_differential(df, window=5).get("vol_diff_pct")
                if is_num(v_diff):
                    diffs.append(float(v_diff))
        if diffs:
            flows[sector] = round(sum(diffs) / len(diffs), 2)
    return flows


def _flow_label(pct: float) -> str:
    if pct >= 15:
        return "akumulasi / volume meningkat"
    if pct <= -15:
        return "volume mengering / melandai"
    return "volume stabil / netral"


# --------------------------------------------------------------------------- #
# Gemini synthesis
# --------------------------------------------------------------------------- #
SYSTEM_INSTRUCTION = """\
Anda adalah Institutional Equity & Macro Strategist senior yang memantau Bursa Efek Indonesia (BEI / IDX).
Tugas Anda adalah menyusun ringkasan pra-pembukaan pasar (pre-market brief) yang bersih, tajam, profesional, dan mudah dibaca cepat di Telegram (layar ponsel).

Gaya Bahasa & Nada:
- Bahasa: SELALU tulis dalam Bahasa Indonesia yang profesional, ringkas, padat, dan institusional.
- Tanpa basa-basi: Jangan gunakan salam pembuka/penutup atau disclaimer klise.
- Insightful: Hubungkan sentimen makro, nilai tukar rupiah, harga komoditas global, serta berita media ke rotasi sektoral di BEI.

Akurasi Data (Grounding):
- Gunakan HANYA data pasar makro, komoditas, arus likuiditas sektoral, dan berita yang disediakan. Jangan mengarang angka atau rumor.
- Format mata uang Rupiah: misal "Rp 17.875", level indeks: "6.093,76", komoditas: "$2.650 / oz" atau "$72 / bbl".

Format & Struktur Brief (Wajib TEPAT 3 Bagian Terpisah):
1. *Makro & Pasar Global:*
   Gunakan poin peluru (•) untuk:
   • *IHSG:* level terakhir, % perubahan, dan tren volume 5D.
   • *USD/IDR:* kurs rupiah, % perubahan, serta dampak bagi eksportir vs emiten impor.
   • *Komoditas Global:* pergerakan Emas dan Minyak Mentah serta implikasinya ke pasar domestik.
   • *Likuiditas Sektoral:* sektor mana yang mencatat akumulasi volume tertinggi vs yang mengering.
2. *Radar Berita & Katalis Utama Hari Ini:*
   3–5 poin peluru (•) merangkum intisari berita finansial terpenting dari Kontan, CNBC Indonesia, dan Bisnis.com.
3. *Rotasi & Rekomendasi Sektor Unggulan:*
   - Sebutkan 1-2 sektor terbaik hari ini yang didukung oleh katalis fundamental/makro atau akumulasi likuiditas.
   - Cantumkan 2-3 contoh kode saham leader di sektor tersebut yang menarik untuk diperhatikan.
   - Sertakan ajakan aksi spesifik:
     `💡 *Action Tip:* Ketik /analyze <TICKER> (misal: /analyze BBRI) untuk memicu analisis multi-agen mendalam.`

Aturan Penulisan Telegram:
- Bold judul metrik penting, harga, dan kode saham dengan SATU tanda bintang: *teks*. Jangan gunakan bintang ganda (**).
- Dilarang menggunakan heading markdown (#), code blocks (```), tabel, link markdown, atau garis bawah (_).
- Pisahkan setiap bagian utama dengan tepat 1 baris kosong.
"""

BRIEF_TEMPLATE = """\
🌅 *Morning Briefing - {date}*

*Makro & Pasar Global:*
• *IHSG:* <level indeks> (<% pergerakan>) | <1 frasa ringkas pergerakan / tren volume>
• *USD/IDR:* <kurs rupiah> (<% pergerakan>) — <1 kalimat ringkas dampak ke sektor ekspor vs impor>
• *Komoditas Global:* Emas <harga/tren> | Minyak <harga/tren> — <implikasi ke BEI>
• *Likuiditas Sektoral:* <ringkasan sektor yang mengalami lonjakan volume vs yang melandai>

*Radar Berita & Katalis Utama Hari Ini:*
• *<Sumber Berita>:* <Intisari berita / katalis penting 1>
• *<Sumber Berita>:* <Intisari berita / katalis penting 2>
• *<Sumber Berita>:* <Intisari berita / katalis penting 3>

*Rotasi & Rekomendasi Sektor Unggulan:*
<Emoji Sektor> *<Nama Sektor>:* <Alasan pemilihan sektor berdasarkan arus volume & katalis makro. Sebutkan 2-3 saham leader contoh: *TICKER1*, *TICKER2* yang menarik untuk dianalisis.>

💡 *Action Tip:* Ketik `/analyze <TICKER>` (misal: `/analyze BBRI`) untuk memicu analisis multi-agen mendalam.
"""

STYLE_EXAMPLE = """\
🌅 *Morning Briefing - 12 Okt 2026*

*Makro & Pasar Global:*
• *IHSG:* 6.093,76 (+1,04%) | Rebound teknikal jangka pendek, namun rerata volume 5H melandai (-9,0%)
• *USD/IDR:* Rp 17.875 (+0,38%) — Pelemahan rupiah memberi sentimen positif bagi eksportir energi, namun membebani emiten berbasis utang valas/impor.
• *Komoditas Global:* Emas $2.650/oz (+0,8%) bertahan di level tinggi, Minyak WTI $71,5/bbl (-1,2%) cenderung tertekan.
• *Likuiditas Sektoral:* Arus likuiditas bertahan kuat di Perbankan (+12,5%) dan Energi (+8,4%), sementara Tambang & Mineral mengalami rotasi keluar (-18,2%).

*Radar Berita & Katalis Utama Hari Ini:*
• *Kontan:* Bank Indonesia diperkirakan menahan suku bunga acuan BI-Rate untuk menjaga stabilitas nilai tukar Rupiah.
• *CNBC Indonesia:* Kementerian ESDM rilis evaluasi royalti hilirisasi nikel dan tembaga, sentimen terbagi bagi emiten smelter.
• *Bisnis.com:* Rilis laporan keuangan Q3 perbankan buku IV diproyeksikan mencatat pertumbuhan margin bunga bersih (NIM) stabil.

*Rotasi & Rekomendasi Sektor Unggulan:*
🏦 *Perbankan & Energi:* Didukung oleh stabilnya margin perbankan dan katalis dividen interim energi. Saham leader yang menarik dipantau: *BBRI*, *BMRI*, dan *ADRO*.

💡 *Action Tip:* Ketik `/analyze <TICKER>` (misal: `/analyze BBRI`) untuk memicu analisis multi-agen mendalam.
"""


def build_prompt(now: datetime, macro: dict[str, dict[str, Any]],
                 flows: dict[str, float],
                 headlines: dict[str, list[dict[str, Any]]]) -> str:
    now_day = DAYS_ID[now.weekday()]
    lines: list[str] = [
        f"WAKTU SAAT INI: {now_day}, {fmt_brief_date(now)}, {now:%H:%M} WIB (pra-pembukaan perdagangan BEI sebelum 09:00 WIB).",
        "",
        "RINGKASAN PASAR MAKRO & KOMODITAS (penutupan terakhir):",
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
    gold = macro.get("Gold (Emas)", {})
    lines.append(
        f"- Emas Dunia (GC=F): {fmt_usd(gold.get('close'))} ({fmt_pct(gold.get('change_pct'))} h/h)"
    )
    oil = macro.get("Crude Oil (WTI)", {})
    lines.append(
        f"- Minyak Mentah WTI (CL=F): {fmt_usd(oil.get('close'))} ({fmt_pct(oil.get('change_pct'))} h/h)"
    )

    lines += ["", "ARUS LIKUIDITAS SEKTOR BEI (Rata-rata 5D vs 20D dari Proksi Leader Sektor):"]
    if flows:
        for sector, pct in sorted(flows.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"- {sector}: {fmt_pct(pct)} ({_flow_label(pct)})")
    else:
        lines.append("- N/A")

    lines += ["", "BERITA TERKINI DARI MEDIA KEUANGAN (KONTAN, CNBC, BISNIS):"]
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
        "Tulis pre-market briefing dalam Bahasa Indonesia institusional yang padat, rapi, dan mudah dibaca di ponsel:",
        "1. Makro & Pasar Global (IHSG, USD/IDR, Emas, Minyak, Likuiditas Sektor)",
        "2. Radar Berita & Katalis Utama Hari Ini (3-5 poin berita tajam dari RSS)",
        "3. Rotasi & Rekomendasi Sektor Unggulan (pilih sektor unggulan + 2-3 saham leader + instruksi /analyze <TICKER>)",
        "Output HANYA teks brief sesuai template berikut:",
        "",
        BRIEF_TEMPLATE.format(date=fmt_brief_date(now)),
        "",
        "CONTOH FORMAT & GAYA (angka hanya ilustrasi - JANGAN salin faktanya):",
        STYLE_EXAMPLE,
    ]
    return "\n".join(lines)


def gemini_models() -> list[str]:
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL.lower() != "none" and GEMINI_FALLBACK_MODEL not in models:
        models.append(GEMINI_FALLBACK_MODEL)
    return models


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, genai_errors.ClientError):
        return getattr(exc, "code", None) == 429
    return True


def synthesize_brief(prompt: str, api_key: str, attempts: int = 3) -> str:
    client = genai.Client(api_key=api_key)
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
                    break
                if attempt < attempts:
                    time.sleep(5 * attempt)
        log.warning("Gemini model %s exhausted - trying next model if available.", model)
    raise RuntimeError(f"Gemini synthesis failed on {gemini_models()}") from last_exc


def sanitize_markdown(text: str) -> str:
    text = text.replace("```", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^\s*#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"^(\s*)\*\s+", r"\1- ", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def enforce_brief_format(text: str, header: str) -> str:
    text = sanitize_markdown(text)
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("🌅"):
        lines[0] = header
        return "\n".join(lines)
    return f"{header}\n\n{text}"


# --------------------------------------------------------------------------- #
# Deterministic fallback brief
# --------------------------------------------------------------------------- #
def build_fallback_brief(header: str, macro: dict[str, dict[str, Any]],
                         headlines: dict[str, list[dict[str, Any]]],
                         flows: dict[str, float] | None = None) -> str:
    ihsg = macro.get("IHSG", {})
    fx = macro.get("USD/IDR", {})
    gold = macro.get("Gold (Emas)", {})
    oil = macro.get("Crude Oil (WTI)", {})

    parts = [
        header,
        "",
        "⚠️ *Sintesis AI tidak tersedia - ringkasan data pasar deterministik.*",
        "",
        "*Makro & Pasar Global:*",
        f"• *IHSG:* {fmt_index(ihsg.get('close'))} ({fmt_pct(ihsg.get('change_pct'))}) | Vol 5D: {fmt_pct(ihsg.get('vol_diff_pct'))}",
        f"• *USD/IDR:* {fmt_rp(fx.get('close'))} ({fmt_pct(fx.get('change_pct'))})",
        f"• *Komoditas Global:* Emas {fmt_usd(gold.get('close'))} ({fmt_pct(gold.get('change_pct'))}) | Minyak {fmt_usd(oil.get('close'))} ({fmt_pct(oil.get('change_pct'))})",
    ]
    if flows:
        flow_parts = [f"{sec} ({fmt_pct(pct)})" for sec, pct in sorted(flows.items(), key=lambda kv: kv[1], reverse=True)]
        parts.append(f"• *Likuiditas Sektoral:* " + ", ".join(flow_parts[:4]))

    top_news = [item for items in headlines.values() for item in items[:2]]
    if top_news:
        parts += ["", "*Radar Berita & Katalis Utama Hari Ini:*"]
        for i in top_news[:4]:
            parts.append(f"• *{escape_md(i['source'])}:* {escape_md(i['title'])}")

    top_sector = max(flows.items(), key=lambda kv: kv[1])[0] if flows else "Perbankan"
    proxies = ", ".join([t.replace(".JK", "") for t in SECTOR_LEADER_PROXIES.get(top_sector, ["BBCA", "BBRI"])[:3]])
    parts += [
        "",
        "*Rotasi & Rekomendasi Sektor Unggulan:*",
        f"📊 *{top_sector}:* Menunjukkan momentum likuiditas terbaik ({fmt_pct(flows.get(top_sector, 0.0)) if flows else 'N/A'}). Saham leader: *{proxies}*.",
        "",
        "💡 *Action Tip:* Ketik `/analyze <TICKER>` (misal: `/analyze BBRI`) untuk memicu analisis multi-agen mendalam.",
    ]
    return "\n".join(parts)


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
        while len(line) > limit:
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

    log.info("Building pure macro & sector morning brief for %s WIB", now.strftime("%Y-%m-%d %H:%M"))

    headlines = gather_headlines()
    macro = {label: summarize_symbol(sym) for label, sym in MACRO_SYMBOLS.items()}
    flows = calculate_sector_liquidity_flows()

    header = f"🌅 *Morning Briefing - {fmt_brief_date(now)}*"
    brief: str | None = None
    if gemini_key:
        try:
            prompt = build_prompt(now, macro, flows, headlines)
            brief = enforce_brief_format(synthesize_brief(prompt, gemini_key), header)
        except Exception as exc:  # noqa: BLE001
            log.error("AI synthesis failed, falling back to data snapshot: %s", exc)
    else:
        log.error("GEMINI_API_KEY is not set - sending data-only fallback brief.")

    if not brief:
        brief = build_fallback_brief(header, macro, headlines, flows=flows)

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
