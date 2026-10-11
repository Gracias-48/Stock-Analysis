# System Instructions: Dual-Engine IDX Stock Tracker

## 1. Project Overview
Build a "Personal Autonomous Stock Intelligence & Alert System" for the Indonesia Stock Exchange (IDX). The system monitors a personal watchlist, synthesizes Indonesian financial news using the Google Gemini API, and sends alerts via Telegram.

The system relies on three files:
1. `watchlist.json`: A static configuration file storing stock tickers, targets, and sectors.
2. `morning_brief.py`: An AI-driven summarization engine.
3. `price_sentry.py`: An intraday technical and price-level monitoring engine.

The architecture is designed to be fully serverless, relying on **GitHub Actions** cron jobs for execution. No database is required; state is managed via a temporary daily log file.

## 2. Dependencies & API Setup
*   **Python Version:** 3.10+
*   **Required Packages:** `yfinance`, `pandas`, `pandas-ta`, `feedparser`, `requests`, `google-genai`.
    *   *Critical Note on Gemini:* Use the current `google-genai` SDK, **not** the legacy `google-generativeai` package. Initialize the client using `from google import genai` and `client = genai.Client(api_key=GEMINI_API_KEY)`.
*   **API Keys (Passed via Environment Variables):**
    *   `BOT_TOKEN`: Telegram Bot Token.
    *   `CHAT_ID`: Telegram Chat ID.
    *   `GEMINI_API_KEY`: Google Gemini API key.

## 3. Data Formatting Rules
*   **IDX Tickers:** All stock symbols must be appended with `.JK` for Yahoo Finance compatibility (e.g., `BBRI.JK`).
*   **Currency & Time:** Format all currency as Indonesian Rupiah with thousand separators (e.g., `Rp 4,500`). Use WIB (UTC+7) for time context in prompts.
*   **Telegram Markdown:** Use strict, clean Markdown for Telegram payloads. Use emojis heavily for visual scanning (🚨, 🎯, 📉).

## 4. `morning_brief.py` Specifications
**Trigger Schedule:** 08:30 WIB (01:30 UTC), Monday–Friday.
**Objective:** Deliver an AI-synthesized pre-market brief.

*   **Data Aggregation Tasks:**
    1.  **RSS Feed Discovery:** Please research and find the best, most current active RSS feed URLs for top Indonesian financial news portals (specifically target **Kontan**, **CNBC Indonesia**, and **Bisnis.com**). Parse these feeds to extract the top 5 latest headlines from each.
        *   *(Note: Implement a `try-except` block when parsing feeds so the script does not crash if one of your chosen URLs is temporarily down).*
    2.  Fetch last closing prices for the watchlist, `^JKSE` (IHSG), and `IDR=X` (USD/IDR).
    3.  Calculate a 2-week volume differential (current 5-day moving average volume vs. previous 5-day moving average volume) using `yfinance` history.
*   **AI Synthesis Task:**
    *   Pass the aggregated data to Gemini (model: `gemini-3.8-flash`, falling back to `gemini-3.1-pro-preview` if the primary model fails).
    *   Prompt the AI to output a 3-part brief: (1) Macro & Momentum, (2) Explicit Sector Recommendation, and (3) Key Catalysts for the user's specific stocks.
*   **Output:** Send the generated text to the Telegram API.

## 5. `price_sentry.py` Specifications
**Trigger Schedule:** Every 15 minutes during IDX regular trading hours:
*   Morning Session: 09:00 - 12:00 WIB (02:00 - 05:00 UTC)
*   Afternoon Session: 13:30 - 15:49 WIB (06:30 - 08:49 UTC)
*   *(Configure cron to run `*/15 2-9 * * 1-5` and handle breaks in code if desired).*

**Objective:** Send actionable alerts without spamming the user.

*   **Logic Tasks:**
    1.  Fetch 60 days of historical data via `yfinance` to calculate a 14-period RSI and 20-period SMA using `pandas_ta`.
    2.  Check current price against the `buy_below` and `sell_above` values in `watchlist.json`.
    3.  Check if RSI is `< 30` (Oversold).
*   **State Management (De-duplication):**
    *   To prevent spam, maintain a local text file (`daily_alerts.log`).
    *   Format: `YYYY-MM-DD|TICKER_ALERTTYPE` (e.g., `2026-10-12|ANTM.JK_price_buy`).
    *   Before sending an alert, check if that exact ticker and alert type has already fired today. If it has, skip it. If the date in the file is not today, clear the file.
*   **Output:** Send a short Telegram alert if conditions are met.

***

# Brand Guidelines: Telegram Alert Styling

When programming the output strings and the Gemini prompt instructions, enforce the following visual style for Telegram messages.

## 1. Tone and Voice
*   **Objective, Institutional, Concise.** No fluff, greetings, or conversational filler.
*   **Action-Oriented.** Tell the user exactly what threshold was breached or what the AI recommends.

## 2. Visual Hierarchy & Emojis
Use standard emojis as visual tags so the user can identify the alert type from the lock screen.

*   **Morning Brief:** 🌅 `*Morning Briefing*`
*   **Buy Alert:** 🚨 `*BUY ALERT: [TICKER]*`
*   **Sell Alert:** 🎯 `*SELL ALERT: [TICKER]*`
*   **Technical/Oversold:** 📉 `*TECHNICAL ALERT: [TICKER]*`
*   **Dividend/Corporate:** 💰 `*DIVIDEND RADAR*`

## 3. Spacing and Formatting
*   Use `*` for bolding key metrics and ticker names.
*   Separate distinct thoughts with line breaks.
*   Do not use code blocks (```) for regular text as they wrap poorly on mobile.

## 4. Example Output Templates

**Price Sentry Template:**
> 🚨 *BUY ALERT: ANTM*
> Price dropped to Rp 1,290 
> (Target: < Rp 1,300)

**Technical Sentry Template:**
> 📉 *TECHNICAL ALERT: MDKA OVERSOLD*
> RSI is currently at 28.4.
> Price is Rp 2,150. Consider looking for entry setups.

**Morning Brief Template Requirements:**
Ensure the Gemini prompt strictly demands this format:
> 🌅 *Morning Briefing - Oct 12, 2026*
> 
> *Macro & Momentum:*
> IHSG is at 7,200. USD/IDR is hovering at Rp 15,500, creating tailwinds for export miners. Volume analysis shows strong institutional accumulation in banking, while coal volumes are drying up.
> 
> *Recommendation:*
> 🏦 *Banking.* Prioritize accumulating BBRI. Foreign flow is stabilizing and the P/E ratio is historically cheap ahead of the Q3 earnings cycle.
> 
> *Key Catalysts:*
> - **ANTM:** Nickel prices dropped 2% overnight in London.
> - **DSSA:** Company announced potential expansion into renewable assets.