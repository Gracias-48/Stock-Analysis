# System Instructions: Triple-Engine IDX Stock Intelligence System

## 1. Project Overview
Build a "Personal Autonomous Stock Intelligence & Multi-Agent Alert System" for the Indonesia Stock Exchange (IDX).
The architecture operates on a **Triple-Engine** framework inspired by `TauricResearch/TradingAgents`:

1. **Engine 1 - Macro & Sector Catalyst Radar (`morning_brief.py`):**
   Runs pre-market (08:25 WIB, Mon–Fri). Independent of `watchlist.json`, synthesizes broad macro trends (IHSG, USD/IDR, Gold, Crude Oil), IDX sector liquidity flows (5D vs 20D), top financial headlines, and sector recommendations with an action tip to analyze specific tickers via `/analyze <TICKER>`.
2. **Engine 2 - Intraday Price & Risk Sentry (`price_sentry.py`):**
   Runs every 15 minutes during IDX market hours. Monitors user-configured price levels (`buy_below`, `sell_above`, `stop_loss`) and technical extremes (RSI < 30) from `watchlist.json` with stateful daily de-duplication.
3. **Engine 3 - On-Demand Multi-Agent Deep Analyzer (`deep_agent.py`):**
   Triggered on-demand via Telegram chat command `/analyze <TICKER>` (via Cloudflare Worker) or GitHub Actions `workflow_dispatch`. Executes a LangGraph-orchestrated team of agents (4 Parallel Analysts, Bull vs Bear Researcher Debate, Research Manager & Trader Proposal with Pydantic, and Risk Management / Portfolio Manager 5-Tier Decision) to deliver an institutional deep-dive report.

---

## 2. Dependencies & API Setup
* **Python Version:** 3.12+
* **Required Packages:** `yfinance`, `pandas`, `pandas-ta`, `feedparser`, `requests`, `google-genai`, `langgraph`, `pydantic`.
* **API Keys (Passed via Environment Variables):**
  * `BOT_TOKEN`: Telegram Bot Token.
  * `CHAT_ID`: Telegram Chat ID.
  * `GEMINI_API_KEY`: Google Gemini API key.
  * `TARGET_TICKER`: Target stock ticker for `deep_agent.py` (e.g. `BBRI`).

---

## 3. Data Formatting Rules
* **IDX Tickers:** All stock symbols must be appended with `.JK` for Yahoo Finance compatibility (e.g., `BBRI.JK`), while displayed cleanly without `.JK` in user-facing reports.
* **Currency & Time:** Format IDR as Rupiah with thousand separators (e.g., `Rp 3,100`). Format USD where appropriate (`$2,650`). Use WIB (UTC+7) context for market hours.
* **Telegram Markdown:** Use strict, clean legacy Markdown for Telegram payloads. Use standard emojis as visual tags (🌅, 🚨, 🎯, 🛑, 📉, 📊, 🐂, 🐻, ⚖️).

---

## 4. `morning_brief.py` Specifications
**Trigger Schedule:** 08:25 WIB (01:25 UTC), Monday–Friday.
**Objective:** Deliver an AI-synthesized macro & sector catalyst radar.

* **Data Aggregation:**
  1. Top headlines from Kontan, CNBC Indonesia, and Bisnis.com RSS feeds.
  2. Macro data: IHSG (`^JKSE`), USD/IDR (`IDR=X`), Gold (`GC=F`), Crude Oil (`CL=F`).
  3. Aggregate Sector Liquidity Flows across key IDX sector leader proxies (5D vs 20D volume differential).
* **AI Synthesis Output (3 Sections):**
  1. `*Makro & Pasar Global:*` (IHSG, USD/IDR, Emas, Minyak, Likuiditas Sektoral)
  2. `*Radar Berita & Katalis Utama Hari Ini:*` (3-5 poin berita terpanas)
  3. `*Rotasi & Rekomendasi Sektor Unggulan:*` (Sektor unggulan, 2-3 saham leader, serta ajakan bertindak `/analyze <TICKER>`)

---

## 5. `price_sentry.py` Specifications
**Trigger Schedule:** Every 15 minutes during IDX sessions:
* Morning Session: 09:00 - 12:00 WIB (Mon–Fri)
* Afternoon Session: 13:30 - 15:49 WIB (Mon–Thu) / 14:00 - 15:49 WIB (Fri)
**Objective:** Actionable intraday threshold alerts (`buy_below`, `sell_above`, `stop_loss`, RSI oversold).
**State Management:** De-duplicates alerts using `daily_alerts.log` persisted via GitHub Actions cache.

---

## 6. `deep_agent.py` Specifications (TradingAgents Multi-Agent)
**Trigger:** On-demand via `/analyze <TICKER>` or GitHub Actions `workflow_dispatch`.
**Architecture (LangGraph `StateGraph`):**
1. **4 Parallel Analysts:**
   - *Technical Analyst:* Price, 1D/5D change, SMA(20, 50, 200), EMA(10), RSI(14), MACD, ATR(14), 52-week High/Low distance.
   - *Fundamentals Analyst:* P/E, PBV (with USD currency normalization for miners/energy), ROE, DER, Dividend Yield, Revenue & Net Income growth.
   - *News Analyst:* Emitent-specific RSS news + macro IHSG context.
   - *Volume & Flow Analyst:* Anomaly volume ratio (last day vs 5D and 20D MA), accumulation/distribution indication.
2. **Researcher Debate:**
   - *Bull Researcher:* Strongest evidence-based thesis (catalysts, valuation, momentum).
   - *Bear Researcher:* Strongest counter-thesis (downside risk, valuation trap, debt, resistance).
3. **Research Manager & Trader Proposal:**
   - Mediates debate and creates concrete plan: Entry Buy range, Take Profit (TP), Stop Loss (SL) based on support/resistance and ATR volatility. Reads `watchlist.json` if available.
4. **Risk Management & Portfolio Manager Final Decision:**
   - 5-Tier Rating Scale: `Buy`, `Overweight`, `Hold`, `Underweight`, `Sell`.
5. **Telegram Delivery:** Formats and sends executive report (< 4000 chars per message chunk).