# IDX Stock Intelligence & Multi-Agent Analysis System

Sistem intelijen dan pemantau saham Bursa Efek Indonesia (BEI / IDX) bertenaga AI dengan arsitektur **Triple-Engine Multi-Agent**, terinspirasi dari arsitektur multi-agen **TauricResearch/TradingAgents** dan dioptimalkan untuk eksekusi serverless via GitHub Actions dan Cloudflare Workers.

---

## 🏛️ Arsitektur Triple-Engine

| Mesin | File | Jadwal / Pemicu | Fokus & Output |
|---|---|---|---|
| **Engine 1: Macro & Sector Radar** | `morning_brief.py` | 08:25 WIB (01:25 UTC), Senin–Jumat | 🌅 **Pure Macro, Sector Liquidity Flow & News Catalyst Radar**:<br>Kondisi IHSG (`^JKSE`), USD/IDR (`IDR=X`), Komoditas Emas (`GC=F`) & Minyak Mentah (`CL=F`), Radar Berita (Kontan, CNBC, Bisnis.com), serta Rotasi Sektor & Rekomendasi 2-3 Saham *Leader*. |
| **Engine 2: Real-Time Sentry** | `price_sentry.py` | Setiap 15 menit saat sesi bursa, Senin–Jumat | 🚨 **Real-Time Price & Technical Sentinel**:<br>Memantau emiten dalam `watchlist.json` terhadap batas beli (*buy below*), batas jual (*sell above / take profit*), *stop loss*, dan level RSI(14) oversold (≤30) dengan sistem de-duplikasi harian. |
| **Engine 3: Deep Multi-Agent** | `deep_agent.py` | On-Demand (Telegram `/analyze <TICKER>` atau GitHub `workflow_dispatch`) | 🔬 **LangGraph Deep Stock Intelligence (7-Node Pipeline)**:<br>Analisis mendalam 1 saham BEI mencakup 4 Analis Paralel, Debat Bull vs Bear, Trader Execution Plan (Entry, TP, SL), dan Keputusan Akhir Portfolio Manager (5-Tier Rating Scale). |

---

## 🔬 Engine 3: LangGraph Multi-Agent Workflow (`deep_agent.py`)

Engine ketiga mengadaptasi konsep multi-agen *TauricResearch/TradingAgents* ke dalam alur kerja graf terstruktur (**LangGraph `StateGraph`**):

```
       [START]
          │
          ▼
   1. Technical Analyst   ───▶ RSI(14), SMA(20/50/200), EMA(10), MACD, ATR(14), 52-Week Range
          │
          ▼
   2. Fundamentals Analyst ───▶ P/E, PBV (Normalisasi Kurs USD/IDR), ROE, DER, Div Yield, Growth
          │
          ▼
   3. News Analyst        ───▶ RSS Google News Indonesia emiten spesifik + Konteks IHSG
          │
          ▼
   4. Volume & Flow       ───▶ Volume Anomaly Ratio (vs 5D/20D MA) & Status Akumulasi/Distribusi
          │
          ▼
   5. Researcher Debate   ───▶ 🐂 Bull Researcher (Upside) vs 🐻 Bear Researcher (Downside Risk)
          │
          ▼
   6. Trader Proposal     ───▶ Structured Pydantic Plan (Entry Range, TP, SL, R/R Ratio, Katalis)
          │
          ▼
   7. Portfolio Manager   ───▶ 5-Tier Rating (BUY / OVERWEIGHT / HOLD / UNDERWEIGHT / SELL)
          │
          ▼
   8. Executive Delivery  ───▶ Format pesan Telegram rapi + chunking otomatis + retry mechanism
          │
          ▼
        [END]
```

---

## 📂 Struktur Proyek

```
Stock-Analysis/
├── .github/workflows/
│   ├── morning_brief.yml        # Workflow Engine 1 (Cron 08:25 WIB)
│   ├── price_sentry.yml         # Workflow Engine 2 (Cron tiap 15 mnt)
│   └── deep_agent.yml           # Workflow Engine 3 (workflow_dispatch on-demand)
├── scripts/
│   └── cloudflare_worker.js     # Webhook Telegram -> GitHub Actions REST API (/analyze)
├── tests/
│   └── test_logic.py            # Unit test offline (22 skenario pengujian)
├── deep_agent.py                # Engine 3: LangGraph 7-Node Multi-Agent
├── morning_brief.py             # Engine 1: Pure Macro & Sector Liquidity Radar
├── price_sentry.py              # Engine 2: Real-time Watchlist Sentry
├── watchlist.json               # Daftar pantau emiten untuk Price Sentry
├── requirements.txt             # Dependensi Python 3.12+ (langgraph, pydantic, yfinance, dll.)
└── system_instruction.md        # Prompt & instruksi sistem model Gemini
```

---

## ⚙️ Panduan Setup

### 1. Telegram Bot & Chat ID
1. Buat bot baru via [@BotFather](https://t.me/BotFather) dengan perintah `/newbot`, lalu simpan token bot.
2. Kirim pesan ke bot Anda, lalu buka browser: `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`
3. Salin angka `message.chat.id`.

### 2. Google Gemini API Key
Dapatkan API Key gratis di [Google AI Studio](https://aistudio.google.com/apikey).

### 3. GitHub Secrets & Variables
Buka repositori di GitHub: **Settings → Secrets and variables → Actions**:

**Repository Secrets:**
| Nama Secret | Deskripsi |
|---|---|
| `BOT_TOKEN` | Token bot Telegram |
| `CHAT_ID` | Telegram Chat ID tujuan pengiriman laporan |
| `GEMINI_API_KEY` | API Key Google Gemini (Gemini 2.5/3.8 Flash) |

**Repository Variables (Opsional):**
| Nama Variable | Default | Keterangan |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model AI primer |
| `GEMINI_FALLBACK_MODEL` | `gemini-3.1-pro-preview` | Model AI sekunder jika primer rate-limit |

---

## 📱 Integrasi Telegram On-Demand (`/analyze <TICKER>`)

Anda dapat memicu Engine 3 langsung dari chat Telegram menggunakan Cloudflare Worker gratis (`scripts/cloudflare_worker.js`):

1. Buat Worker baru di dashboard [Cloudflare Workers](https://workers.cloudflare.com/).
2. Salin isi file [scripts/cloudflare_worker.js](scripts/cloudflare_worker.js) ke dalam Cloudflare Worker Editor.
3. Tambahkan **Environment Variables / Secrets** di Settings Worker:
   - `BOT_TOKEN`: Token bot Telegram Anda.
   - `GITHUB_PAT`: GitHub Personal Access Token (permission: `repo` atau `actions:write`).
   - `GITHUB_REPO`: `Username/Repository` (contoh: `Gracias-48/Stock-Analysis`).
   - `ALLOWED_CHAT_ID`: (Opsional) Chat ID Anda untuk membatasi eksekusi hanya dari akun Anda.
4. Set Webhook Telegram ke URL Worker Anda:
   ```bash
   curl -F "url=https://<nama-worker>.<subdomain>.workers.dev" https://api.telegram.org/bot<BOT_TOKEN>/setWebhook
   ```
5. Sekarang di Telegram Anda cukup ketik:
   ```
   /analyze BBRI
   /analyze ANTM
   /analyze ASII
   ```
   Bot akan membalas seketika *"⏳ Tim Agen Multi-Agent sedang menganalisis..."* dan laporan lengkap hasil olahan 7 node agen akan dikirim otomatis dalam 1–2 menit!

---

## 🧪 Pengujian Manual & Lokal

### 1. Jalankan Seluruh Unit Test Offline
```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

### 2. Uji Coba Deep Agent Secara Lokal
```powershell
# Dry run tanpa mengirim ke Telegram
$env:DRY_RUN = "1"; .\.venv\Scripts\python.exe deep_agent.py BBRI
$env:DRY_RUN = "1"; .\.venv\Scripts\python.exe deep_agent.py ANTM

# Kirim langsung ke Telegram
$env:BOT_TOKEN = "xxx"; $env:CHAT_ID = "xxx"; $env:GEMINI_API_KEY = "xxx"
.\.venv\Scripts\python.exe deep_agent.py BBRI
```

### 3. Uji Coba Morning Brief & Price Sentry
```powershell
# Morning Brief (Dry run)
$env:DRY_RUN = "1"; .\.venv\Scripts\python.exe morning_brief.py

# Price Sentry (Force run di luar jam bursa)
$env:DRY_RUN = "1"; $env:FORCE_RUN = "1"; .\.venv\Scripts\python.exe price_sentry.py
```

### 4. Pemicu Manual via GitHub Actions
Buka tab **Actions** di repositori GitHub Anda:
- Pilih **Deep Multi-Agent Stock Analysis** → **Run workflow** → Masukkan kode saham (misal: `ANTM`, `BBRI`, `ASII`).
- Pilih **Morning Brief** → **Run workflow** (dapat mencentang `dry_run`).
- Pilih **Price Sentry** → **Run workflow** (dapat mencentang `force_run`).

---

## 🛡️ Catatan Teknis & Ketahanan Sistem

- **Normalisasi Valuasi Lintas Mata Uang (Dual Currency):** Emiten energi/tambang BEI tertentu (misal: DSSA, CUAN, MDKA) membukukan laporan keuangan dalam USD namun harga saham dalam IDR. `deep_agent.py` secara otomatis menormalisasi rasio PBV menggunakan kurs USD/IDR jika `priceToBook > 500` atau `financialCurrency == 'USD'`.
- **Deterministic Fallbacks:** Apabila terjadi pemadaman kuota Gemini API atau gangguan koneksi RSS, sistem tidak akan mengalami *crash*, melainkan beralih otomatis ke *deterministic rules* berbasis indikator teknikal SMA/ATR dan data historis.
- **De-duplikasi Notifikasi:** `price_sentry.py` mencatat riwayat ke `daily_alerts.log` dengan cache GitHub Actions, memastikan satu emiten tidak mengirim sinyal berulang dalam hari perdagangan yang sama.
