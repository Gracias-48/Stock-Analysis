#!/usr/bin/env python3
"""
deep_agent.py - LangGraph-orchestrated Multi-Agent Deep Stock Analyzer for IDX.
Inspired by TauricResearch/TradingAgents architecture.

Architecture & StateGraph:
--------------------------
1. 4 Parallel Domain Analysts:
   - Technical Analyst: Price Action, 1D/5D, SMA(20,50,200), EMA(10), RSI(14), MACD, ATR(14), 52W Range.
   - Fundamentals Analyst: P/E, Forward P/E, PBV (normalized for dual-currency), ROE, DER, Div Yield, Growth.
   - News Analyst: Ticker-specific Google News RSS + Macro IHSG context.
   - Volume & Flow Analyst: Volume anomaly ratio (vs 5D/20D MA), accumulation/distribution indication.
2. Researcher Debate:
   - Bull Researcher: Best conviction thesis (catalysts, undervaluation, technical setup).
   - Bear Researcher: Best counter-thesis (downside risks, value traps, resistance, debt).
3. Research Manager & Trader Proposal (Pydantic Structured Output):
   - Mediates debate into concrete trade plan: Entry range, TP, SL, Risk/Reward (reads watchlist.json if present).
4. Risk Management & Portfolio Manager Final Decision:
   - 5-Tier Rating Scale: BUY | OVERWEIGHT | HOLD | UNDERWEIGHT | SELL with horizon and allocation advice.
5. Telegram Executive Delivery:
   - Formatted for mobile Telegram reading with message chunking and delivery retry.
"""

from __future__ import annotations

import argparse
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
from typing import Any, TypedDict

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
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Configuration & Setup
# --------------------------------------------------------------------------- #
WIB = timezone(timedelta(hours=7), name="WIB")
BASE_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = BASE_DIR / "watchlist.json"

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip() or "gemini-3.1-pro-preview"
HTTP_TIMEOUT = 20
TELEGRAM_MAX_LEN = 4000
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [deep_agent] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("deep_agent")


# --------------------------------------------------------------------------- #
# Pydantic Schemas for Structured Output
# --------------------------------------------------------------------------- #
class TraderPlan(BaseModel):
    action: str = Field(description="Rekomendasi aksi (e.g. Akumulasi Beli, Buy on Weakness, Hold / Wait and See, Profit Taking, Sell / Stop Loss)")
    entry_range: str = Field(description="Rentang harga beli ideal (e.g. Rp 3.050 - Rp 3.120)")
    target_price: float = Field(description="Target harga jual / Take Profit (TP)")
    stop_loss: float = Field(description="Batas proteksi risiko / Stop Loss (SL)")
    risk_reward_ratio: float = Field(description="Rasio potensi keuntungan terhadap risiko (e.g. 2.5)")
    key_catalyst: str = Field(description="Katalis utama pendorong harga dalam 1-2 kalimat")
    primary_risk: str = Field(description="Risiko penurunan utama dalam 1-2 kalimat")


class PortfolioDecision(BaseModel):
    rating: str = Field(description="Rating 5-tier: BUY, OVERWEIGHT, HOLD, UNDERWEIGHT, SELL")
    time_horizon: str = Field(description="Horizon investasi (e.g. 1-4 minggu / Swing Trade, 1-3 bulan / Position Trade)")
    allocation_advice: str = Field(description="Saran porsi alokasi modal (e.g. 5-10% dari total portofolio)")
    strategic_summary: str = Field(description="Kesimpulan strategis ringkas dalam 2-3 kalimat tajam")


def normalize_ticker(raw: str) -> tuple[str, str]:
    cleaned = (raw or "").strip().upper()
    if cleaned.endswith(".JK"):
        cleaned = cleaned[:-3]
    return cleaned, f"{cleaned}.JK"


# --------------------------------------------------------------------------- #
# State Definition
# --------------------------------------------------------------------------- #
class AgentState(TypedDict, total=False):
    ticker: str
    symbol: str
    company_name: str
    sector: str
    as_of: str
    watchlist_entry: dict[str, Any] | None
    technical: dict[str, Any]
    fundamentals: dict[str, Any]
    news: list[dict[str, Any]]
    volume_flow: dict[str, Any]
    macro_context: dict[str, Any]
    bull_thesis: str
    bear_thesis: str
    trader_plan: dict[str, Any]
    portfolio_decision: dict[str, Any]
    report_text: str


# --------------------------------------------------------------------------- #
# Helper Functions
# --------------------------------------------------------------------------- #
def is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(value)


def fmt_rp(value: float | None) -> str:
    return f"Rp {value:,.0f}" if is_num(value) else "N/A"


def fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if is_num(value) else "N/A"


def fmt_volume(value: float | None) -> str:
    if not is_num(value):
        return "N/A"
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= divisor:
            return f"{value / divisor:,.1f}{suffix}"
    return f"{value:,.0f}"


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


def load_watchlist_entry(ticker: str) -> dict[str, Any] | None:
    if not WATCHLIST_PATH.exists():
        return None
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        stocks = data.get("stocks", []) if isinstance(data, dict) else data
        for s in stocks:
            if str(s.get("ticker", "")).strip().upper() == ticker:
                return s
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read watchlist.json: %s", exc)
    return None


def gemini_models() -> list[str]:
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL.lower() != "none" and GEMINI_FALLBACK_MODEL not in models:
        models.append(GEMINI_FALLBACK_MODEL)
    return models


def call_gemini(prompt: str, system_instruction: str = "", attempts: int = 3) -> str:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    client = genai.Client(api_key=api_key)
    config = types.GenerateContentConfig(system_instruction=system_instruction) if system_instruction else None

    last_exc: Exception | None = None
    for model in gemini_models():
        for attempt in range(1, attempts + 1):
            try:
                response = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                text = (response.text or "").strip()
                if text:
                    return text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                log.warning("Gemini %s attempt %d/%d failed: %s", model, attempt, attempts, exc)
                if isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) != 429:
                    break
                if attempt < attempts:
                    time.sleep(3 * attempt)
    raise RuntimeError(f"Gemini call failed on {gemini_models()}") from last_exc


# --------------------------------------------------------------------------- #
# Analyst Nodes
# --------------------------------------------------------------------------- #
def technical_analyst(state: AgentState) -> dict[str, Any]:
    symbol = state["symbol"]
    log.info("Running Technical Analyst for %s...", symbol)
    tech: dict[str, Any] = {
        "price": None, "change_1d": None, "change_5d": None,
        "sma_20": None, "sma_50": None, "sma_200": None, "ema_10": None,
        "rsi_14": None, "macd_line": None, "macd_signal": None, "macd_hist": None,
        "atr_14": None, "high_52w": None, "low_52w": None,
        "dist_52w_high_pct": None, "dist_52w_low_pct": None,
    }

    try:
        df = yf.Ticker(symbol).history(period="1y", interval="1d", auto_adjust=False, actions=False)
        if df is not None and not df.empty and "Close" in df.columns:
            df = df.dropna(subset=["Close"])
            if "Volume" in df.columns and (df["Volume"] > 0).any():
                df = df[df["Volume"] > 0]

            closes = df["Close"].astype(float)
            highs = df["High"].astype(float) if "High" in df.columns else closes
            lows = df["Low"].astype(float) if "Low" in df.columns else closes

            price = float(closes.iloc[-1])
            tech["price"] = price
            state["as_of"] = df.index[-1].strftime("%Y-%m-%d")

            if len(closes) >= 2:
                prev = float(closes.iloc[-2])
                tech["change_1d"] = (price - prev) / prev * 100 if prev > 0 else 0.0
            if len(closes) >= 6:
                prev_5d = float(closes.iloc[-6])
                tech["change_5d"] = (price - prev_5d) / prev_5d * 100 if prev_5d > 0 else 0.0

            # Technical indicators via pandas_ta
            tech["sma_20"] = float(ta.sma(closes, length=20).iloc[-1]) if len(closes) >= 20 else None
            tech["sma_50"] = float(ta.sma(closes, length=50).iloc[-1]) if len(closes) >= 50 else None
            tech["sma_200"] = float(ta.sma(closes, length=200).iloc[-1]) if len(closes) >= 200 else None
            tech["ema_10"] = float(ta.ema(closes, length=10).iloc[-1]) if len(closes) >= 10 else None
            tech["rsi_14"] = float(ta.rsi(closes, length=14).iloc[-1]) if len(closes) >= 14 else None

            if len(closes) >= 30:
                macd_df = ta.macd(closes)
                if macd_df is not None and not macd_df.empty:
                    tech["macd_line"] = float(macd_df["MACD_12_26_9"].iloc[-1])
                    tech["macd_signal"] = float(macd_df["MACDs_12_26_9"].iloc[-1])
                    tech["macd_hist"] = float(macd_df["MACDh_12_26_9"].iloc[-1])

            if len(df) >= 14:
                atr_s = ta.atr(highs, lows, closes, length=14)
                if atr_s is not None and not atr_s.empty:
                    tech["atr_14"] = float(atr_s.iloc[-1])

            # 52-week High/Low
            high_52w = float(highs.max())
            low_52w = float(lows.min())
            tech["high_52w"] = high_52w
            tech["low_52w"] = low_52w
            if high_52w > 0:
                tech["dist_52w_high_pct"] = (price - high_52w) / high_52w * 100
            if low_52w > 0:
                tech["dist_52w_low_pct"] = (price - low_52w) / low_52w * 100
    except Exception as exc:  # noqa: BLE001
        log.warning("Technical analysis failed for %s: %s", symbol, exc)

    return {"technical": tech}


def fundamentals_analyst(state: AgentState) -> dict[str, Any]:
    symbol = state["symbol"]
    log.info("Running Fundamentals Analyst for %s...", symbol)
    fund: dict[str, Any] = {
        "company_name": state.get("ticker", ""),
        "sector": "N/A", "industry": "N/A",
        "market_cap_t": None, "pe": None, "forward_pe": None, "pbv": None,
        "roe": None, "der": None, "div_yield": None,
        "revenue_growth": None, "earnings_growth": None,
    }

    try:
        info = yf.Ticker(symbol).info or {}
        fund["company_name"] = info.get("longName") or info.get("shortName") or state["ticker"]
        fund["sector"] = info.get("sector") or "N/A"
        fund["industry"] = info.get("industry") or "N/A"

        mcap = info.get("marketCap")
        if is_num(mcap) and mcap > 0:
            fund["market_cap_t"] = round(float(mcap) / 1e12, 1)  # Trillion IDR

        pe = info.get("trailingPE")
        if is_num(pe) and pe > 0:
            fund["pe"] = round(float(pe), 2)
        f_pe = info.get("forwardPE")
        if is_num(f_pe) and f_pe > 0:
            fund["forward_pe"] = round(float(f_pe), 2)

        raw_pbv = info.get("priceToBook")
        if is_num(raw_pbv) and raw_pbv > 0:
            # Fix USD/IDR currency mismatch for miners/energy (MDKA, DSSA, CUAN)
            if info.get("financialCurrency") == "USD" or raw_pbv > 500:
                fund["pbv"] = round(float(raw_pbv) / 18000.0, 2)
            else:
                fund["pbv"] = round(float(raw_pbv), 2)

        roe = info.get("returnOnEquity")
        if is_num(roe):
            fund["roe"] = round(float(roe) * 100, 2)

        der = info.get("debtToEquity")
        if is_num(der):
            fund["der"] = round(float(der) / 100, 2)

        div_y = info.get("dividendYield")
        if is_num(div_y):
            fund["div_yield"] = round(float(div_y) if float(div_y) > 1.0 else float(div_y) * 100, 2)

        rev_g = info.get("revenueGrowth")
        if is_num(rev_g):
            fund["revenue_growth"] = round(float(rev_g) * 100, 2)

        earn_g = info.get("earningsGrowth")
        if is_num(earn_g):
            fund["earnings_growth"] = round(float(earn_g) * 100, 2)
    except Exception as exc:  # noqa: BLE001
        log.warning("Fundamentals analysis failed for %s: %s", symbol, exc)

    return {
        "fundamentals": fund,
        "company_name": fund["company_name"],
        "sector": fund["sector"],
    }


def news_analyst(state: AgentState) -> dict[str, Any]:
    ticker = state["ticker"]
    log.info("Running News Analyst for %s...", ticker)
    news_items: list[dict[str, Any]] = []

    # Emitent-specific RSS search
    url = f"https://news.google.com/rss/search?q={ticker}+saham+indonesia&hl=id&gl=ID&ceid=ID:id"
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
        if resp.ok:
            feed = feedparser.parse(resp.content)
            for entry in feed.entries[:6]:
                title = clean_text(entry.get("title"), 180)
                title = re.split(r"\s+-\s+Bisnis|\s+-\s+CNBC|\s+-\s+Kontan|\s+-\s+Detik", title)[0].strip()
                if title:
                    news_items.append({"title": title})
    except Exception as exc:  # noqa: BLE001
        log.warning("News fetch failed for %s: %s", ticker, exc)

    # Macro IHSG context
    macro_context = {"ihsg_close": None, "ihsg_change": None}
    try:
        ihsg_df = yf.Ticker("^JKSE").history(period="5d")
        if ihsg_df is not None and len(ihsg_df) >= 2:
            c = float(ihsg_df["Close"].iloc[-1])
            p = float(ihsg_df["Close"].iloc[-2])
            macro_context["ihsg_close"] = c
            macro_context["ihsg_change"] = (c - p) / p * 100 if p > 0 else 0.0
    except Exception as exc:  # noqa: BLE001
        log.warning("Macro IHSG fetch failed: %s", exc)

    return {"news": news_items, "macro_context": macro_context}


def volume_flow_analyst(state: AgentState) -> dict[str, Any]:
    symbol = state["symbol"]
    log.info("Running Volume & Flow Analyst for %s...", symbol)
    flow: dict[str, Any] = {
        "vol_last": None, "vol_5d_ma": None, "vol_20d_ma": None,
        "vol_ratio_20d": None, "flow_diagnosis": "Netral",
    }

    try:
        df = yf.Ticker(symbol).history(period="60d", interval="1d")
        if df is not None and "Volume" in df.columns:
            volumes = df["Volume"].dropna().astype(float)
            if len(volumes) >= 20:
                vol_last = float(volumes.iloc[-1])
                vol_5d = float(volumes.iloc[-5:].mean())
                vol_20d = float(volumes.iloc[-20:].mean())

                flow["vol_last"] = vol_last
                flow["vol_5d_ma"] = vol_5d
                flow["vol_20d_ma"] = vol_20d

                if vol_20d > 0:
                    ratio = vol_last / vol_20d
                    flow["vol_ratio_20d"] = round(ratio, 2)
                    if ratio >= 1.6:
                        flow["flow_diagnosis"] = "Akumulasi Kuat (Volume Surge)"
                    elif ratio >= 1.15:
                        flow["flow_diagnosis"] = "Akumulasi Sedang"
                    elif ratio <= 0.65:
                        flow["flow_diagnosis"] = "Volume Mengering / Sepi"
                    else:
                        flow["flow_diagnosis"] = "Aktivitas Normal / Netral"
    except Exception as exc:  # noqa: BLE001
        log.warning("Volume flow analysis failed for %s: %s", symbol, exc)

    return {"volume_flow": flow}


# --------------------------------------------------------------------------- #
# Debate & Synthesis Nodes
# --------------------------------------------------------------------------- #
def researcher_debate_node(state: AgentState) -> dict[str, Any]:
    ticker = state["ticker"]
    log.info("Executing Bull vs Bear Researcher Debate for %s...", ticker)
    t = state.get("technical", {})
    f = state.get("fundamentals", {})
    v = state.get("volume_flow", {})
    news_titles = "\n".join([f"- {n['title']}" for n in state.get("news", [])[:5]]) or "- Berita umum pasar."

    prompt = f"""\
Anda adalah tim analis riset institusional pasar modal Indonesia yang bertugas mendebat saham {ticker} ({state.get('company_name', '')}).
Data Pasar:
- Harga: Rp {t.get('price')} (1D: {t.get('change_1d')}%, 5D: {t.get('change_5d')}%)
- Teknikal: SMA20: {t.get('sma_20')}, SMA50: {t.get('sma_50')}, SMA200: {t.get('sma_200')}, RSI: {t.get('rsi_14')}, ATR: {t.get('atr_14')}
- Valuasi: P/E: {f.get('pe')}x, PBV: {f.get('pbv')}x, ROE: {f.get('roe')}%, DER: {f.get('der')}x, Div Yield: {f.get('div_yield')}%
- Arus Volume: {v.get('flow_diagnosis')} (Ratio: {v.get('vol_ratio_20d')}x)
- Berita Terkini:
{news_titles}

Tulis debat berbasis bukti yang tajam dalam format JSON persis seperti berikut:
{{
  "bull_thesis": "3-4 poin peluru (•) alasan terkuat mengapa saham ini menarik dibeli (katalis, valuasi murah, momentum volume, atau peluang teknikal).",
  "bear_thesis": "3-4 poin peluru (•) risiko penurunan terkuat (valuasi mahal/value trap, resistance teknikal, utang, atau risiko makro/sektoral)."
}}
Output HANYA JSON murni tanpa markdown code block.
"""
    bull_thesis = "• Valuasi relatif atraktif dengan fundamental solid.\n• Momentum harga berkonsolidasi di atas area support."
    bear_thesis = "• Waspadai volatilitas pasar dan tekanan jual asing jangka pendek.\n• Resistensi teknikal belum ditembus secara meyakinkan."

    try:
        raw = call_gemini(prompt)
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.M)
        parsed = json.loads(cleaned)
        bull_thesis = parsed.get("bull_thesis", bull_thesis)
        bear_thesis = parsed.get("bear_thesis", bear_thesis)
    except Exception as exc:  # noqa: BLE001
        log.warning("Debate synthesis failed; using fallback thesis: %s", exc)

    return {"bull_thesis": bull_thesis, "bear_thesis": bear_thesis}


def trader_proposal_node(state: AgentState) -> dict[str, Any]:
    ticker = state["ticker"]
    log.info("Research Manager & Trader generating Trade Proposal for %s...", ticker)
    t = state.get("technical", {})
    price = float(t.get("price") or 1000.0)
    atr = float(t.get("atr_14") or price * 0.03)
    sma20 = float(t.get("sma_20") or price)
    w = state.get("watchlist_entry")

    prompt = f"""\
Anda adalah Research Manager & Head Trader institusional. Berdasarkan data berikut, susun rencana perdagangan taktis konkret untuk {ticker}:
- Harga Saat Ini: Rp {price}
- ATR Volatilitas (14): Rp {atr:.1f}
- SMA20: Rp {sma20:.1f}
- Watchlist User Target: Buy Below: {w.get('buy_below') if w else 'N/A'}, Sell Above: {w.get('sell_above') if w else 'N/A'}, Stop Loss: {w.get('stop_loss') if w else 'N/A'}
- Bull Thesis: {state.get('bull_thesis')}
- Bear Thesis: {state.get('bear_thesis')}

Hasilkan output JSON valid sesuai schema berikut:
{{
  "action": "Akumulasi Beli / Buy on Weakness / Hold / Profit Taking",
  "entry_range": "Rp ... - Rp ...",
  "target_price": 0.0,
  "stop_loss": 0.0,
  "risk_reward_ratio": 2.5,
  "key_catalyst": "Katalis pendorong utama (1 kalimat)",
  "primary_risk": "Risiko utama yang harus dimitigasi (1 kalimat)"
}}
Target Price dan Stop Loss harus berupa angka floating point / integer bulat (tanpa Rp). Output HANYA JSON murni.
"""
    # Deterministic fallback plan
    target_price = round(price + (1.8 * atr), 0)
    stop_loss = round(price - (1.0 * atr), 0)
    if w and is_num(w.get("sell_above")) and float(w["sell_above"]) > price:
        target_price = float(w["sell_above"])
    if w and is_num(w.get("stop_loss")) and float(w["stop_loss"]) < price:
        stop_loss = float(w["stop_loss"])

    upside = max(target_price - price, 1.0)
    downside = max(price - stop_loss, 1.0)
    rr_ratio = round(upside / downside, 2)

    trader_plan = TraderPlan(
        action="Akumulasi Beli" if price <= (sma20 * 1.02) else "Hold / Buy on Pullback",
        entry_range=f"Rp {price - atr*0.5:,.0f} - Rp {price:,.0f}",
        target_price=target_price,
        stop_loss=stop_loss,
        risk_reward_ratio=rr_ratio,
        key_catalyst="Peluang perbaikan margin dan momentum teknikal di atas batas support.",
        primary_risk="Penembusan ke bawah level stop loss jika sentimen makro memburuk.",
    )

    try:
        raw = call_gemini(prompt)
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.M)
        parsed = json.loads(cleaned)
        trader_plan = TraderPlan.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001
        log.warning("Trader proposal node failed; using deterministic fallback plan: %s", exc)

    return {"trader_plan": trader_plan.model_dump()}


def portfolio_manager_node(state: AgentState) -> dict[str, Any]:
    ticker = state["ticker"]
    log.info("Portfolio Manager rendering final 5-Tier Decision for %s...", ticker)
    plan = state.get("trader_plan", {})
    t = state.get("technical", {})
    f = state.get("fundamentals", {})

    prompt = f"""\
Anda adalah Senior Portfolio Manager yang bertanggung jawab atas keputusan alokasi aset modal.
Evaluasi saham {ticker} melalui 3 perspektif risiko (Aggressive, Neutral, Conservative) dan berikan rating akhir menggunakan skala resmi 5-Tier:
Pilihan Rating: "BUY", "OVERWEIGHT", "HOLD", "UNDERWEIGHT", atau "SELL".

Data:
- Trader Action: {plan.get('action')}
- Target Jual: Rp {plan.get('target_price')}, Stop Loss: Rp {plan.get('stop_loss')}
- Rasio P/E: {f.get('pe')}x, PBV: {f.get('pbv')}x, RSI: {t.get('rsi_14')}

Hasilkan output JSON valid:
{{
  "rating": "OVERWEIGHT",
  "time_horizon": "2-6 minggu (Swing Trade)",
  "allocation_advice": "Alokasi 5-8% dari total portofolio saham",
  "strategic_summary": "2-3 kalimat tajam menyimpulkan kelayakan investasi saham ini."
}}
Output HANYA JSON murni.
"""
    decision = PortfolioDecision(
        rating="OVERWEIGHT" if (f.get("pe") and float(f["pe"]) < 20) else "HOLD",
        time_horizon="2-6 minggu (Swing to Position Trade)",
        allocation_advice="Alokasi maksimal 5-8% dari total modal",
        strategic_summary=f"Secara fundamental {ticker} memiliki valuasi yang wajar dengan risiko terukur. Disarankan masuk bertahap dengan mematuhi level stop loss.",
    )

    try:
        raw = call_gemini(prompt)
        cleaned = re.sub(r"^```json\s*|\s*```$", "", raw.strip(), flags=re.M)
        parsed = json.loads(cleaned)
        decision = PortfolioDecision.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001
        log.warning("Portfolio manager node failed; using fallback decision: %s", exc)

    return {"portfolio_decision": decision.model_dump()}


def report_assembler_node(state: AgentState) -> dict[str, Any]:
    ticker = state["ticker"]
    t = state.get("technical", {})
    f = state.get("fundamentals", {})
    v = state.get("volume_flow", {})
    plan = state.get("trader_plan", {})
    dec = state.get("portfolio_decision", {})
    news_items = state.get("news", [])

    news_text = ""
    for n in news_items[:3]:
        news_text += f"• {escape_md(n['title'])}\n"
    if not news_text:
        news_text = "• Belum ada berita spesifik terbaru hari ini.\n"

    der_str = f"{f.get('der')}x" if is_num(f.get("der")) else "N/A"
    div_str = f"{f.get('div_yield')}%" if is_num(f.get("div_yield")) else "N/A"
    roe_str = f"{f.get('roe')}%" if is_num(f.get("roe")) else "N/A"
    rsi_str = f"{t.get('rsi_14'):.1f}" if is_num(t.get("rsi_14")) else "N/A"

    msg = f"""\
🔬 *DEEP-DIVE STOCK INTELLIGENCE: {ticker}*
*{escape_md(state.get('company_name', ticker))}* | Sektor: {escape_md(state.get('sector', 'N/A'))}
━━━━━━━━━━━━━━━━━━━━━
📊 *Snapshot Harga:* {fmt_rp(t.get('price'))} ({fmt_pct(t.get('change_1d'))} 1H | {fmt_pct(t.get('change_5d'))} 5H)
• *Teknikal:* RSI(14) {rsi_str} | SMA20 {fmt_rp(t.get('sma_20'))} | SMA50 {fmt_rp(t.get('sma_50'))} | SMA200 {fmt_rp(t.get('sma_200'))} | ATR(14) {fmt_rp(t.get('atr_14'))}
• *Valuasi:* P/E {f.get('pe', 'N/A')}x | PBV {f.get('pbv', 'N/A')}x | ROE {roe_str} | DER {der_str} | Div Yield {div_str}
• *Arus & Likuiditas:* {v.get('flow_diagnosis', 'N/A')} (Rasio {v.get('vol_ratio_20d', 'N/A')}x vs 20H) | 52W: {fmt_rp(t.get('low_52w'))} - {fmt_rp(t.get('high_52w'))}

📰 *Katalis Berita Terkini:*
{news_text}
━━━━━━━━━━━━━━━━━━━━━
🐂 *Bull Researcher Thesis (Peluang Upside):*
{state.get('bull_thesis', '')}

🐻 *Bear Researcher Thesis (Risiko Downside):*
{state.get('bear_thesis', '')}

━━━━━━━━━━━━━━━━━━━━━
🎯 *Rencana Perdagangan (Trader Plan):*
• *Rekomendasi:* *{plan.get('action', 'Hold')}*
• *Area Beli (Entry):* {plan.get('entry_range', 'N/A')}
• *Target Jual (TP):* {fmt_rp(plan.get('target_price'))}
• *Batas Proteksi (SL):* {fmt_rp(plan.get('stop_loss'))}
• *Risk/Reward:* 1 : {plan.get('risk_reward_ratio', 2.0)}
• *Katalis Utama:* {escape_md(plan.get('key_catalyst', ''))}
• *Risiko Utama:* {escape_md(plan.get('primary_risk', ''))}

⚖️ *Keputusan Akhir Portfolio Manager:*
⭐ *RATING:* *{dec.get('rating', 'HOLD')}*
• *Horizon:* {dec.get('time_horizon', '1-4 minggu')}
• *Alokasi:* {dec.get('allocation_advice', 'Maksimal 5%')}
• *Kesimpulan Strategis:* {escape_md(dec.get('strategic_summary', ''))}
━━━━━━━━━━━━━━━━━━━━━
"""
    return {"report_text": msg.strip()}


# --------------------------------------------------------------------------- #
# StateGraph Construction
# --------------------------------------------------------------------------- #
def build_deep_agent_graph() -> Any:
    workflow = StateGraph(AgentState)

    workflow.add_node("technical_analyst", technical_analyst)
    workflow.add_node("fundamentals_analyst", fundamentals_analyst)
    workflow.add_node("news_analyst", news_analyst)
    workflow.add_node("volume_flow_analyst", volume_flow_analyst)
    workflow.add_node("researcher_debate", researcher_debate_node)
    workflow.add_node("trader_proposal", trader_proposal_node)
    workflow.add_node("portfolio_manager", portfolio_manager_node)
    workflow.add_node("report_assembler", report_assembler_node)

    # Graph routing: 4 analysts -> debate -> trader proposal -> portfolio manager -> report
    workflow.add_edge(START, "technical_analyst")
    workflow.add_edge("technical_analyst", "fundamentals_analyst")
    workflow.add_edge("fundamentals_analyst", "news_analyst")
    workflow.add_edge("news_analyst", "volume_flow_analyst")
    workflow.add_edge("volume_flow_analyst", "researcher_debate")
    workflow.add_edge("researcher_debate", "trader_proposal")
    workflow.add_edge("trader_proposal", "portfolio_manager")
    workflow.add_edge("portfolio_manager", "report_assembler")
    workflow.add_edge("report_assembler", END)

    return workflow.compile()


# --------------------------------------------------------------------------- #
# Telegram Delivery
# --------------------------------------------------------------------------- #
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
            if resp.ok:
                return
            if resp.status_code == 429:
                wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                time.sleep(wait)
                continue
            if resp.status_code == 400:
                # Fallback to plain text if Telegram rejects markdown
                payload["parse_mode"] = ""
                resp_plain = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
                if resp_plain.ok:
                    return
        except requests.RequestException:
            time.sleep(2 ** attempt)
            continue
    raise RuntimeError("Telegram delivery failed after retries")


def send_telegram(text: str, token: str, chat_id: str) -> None:
    for chunk in split_message(text):
        _send_chunk(chunk, token, chat_id)
    log.info("Deep agent report delivered to Telegram.")


# --------------------------------------------------------------------------- #
# Main Execution
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph Multi-Agent Deep Stock Analyzer for IDX")
    parser.add_argument("ticker", nargs="?", default="", help="Stock ticker to analyze (e.g. BBRI, ANTM, MDKA)")
    parser.add_argument("--dry-run", action="store_true", help="Print report without sending to Telegram")
    args = parser.parse_args()

    raw_ticker = args.ticker or os.getenv("TARGET_TICKER", "").strip() or "BBRI"
    ticker, symbol = normalize_ticker(raw_ticker)

    dry_run = args.dry_run or os.getenv("DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    chat_id = os.getenv("CHAT_ID", "").strip()

    if not dry_run and not (bot_token and chat_id):
        log.error("BOT_TOKEN and CHAT_ID must be set (or use --dry-run / DRY_RUN=1).")
        return 1

    log.info("Starting Multi-Agent Deep Analysis for %s (%s)...", ticker, symbol)
    watchlist_entry = load_watchlist_entry(ticker)

    initial_state: AgentState = {
        "ticker": ticker,
        "symbol": symbol,
        "watchlist_entry": watchlist_entry,
    }

    graph = build_deep_agent_graph()
    final_state = graph.invoke(initial_state)

    report = final_state.get("report_text", "")
    if not report:
        log.error("Failed to generate deep report for %s", ticker)
        return 1

    if dry_run:
        print("\n" + "=" * 60 + "\n" + report + "\n" + "=" * 60)
        return 0

    try:
        send_telegram(report, bot_token, chat_id)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to send deep report to Telegram: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
