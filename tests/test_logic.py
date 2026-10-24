"""Offline logic tests (no network, no Telegram).

Run from the project root:
    .\\.venv\\Scripts\\python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import morning_brief as mb  # noqa: E402
import price_sentry as ps  # noqa: E402
import deep_agent as da  # noqa: E402


class AlertRulesTest(unittest.TestCase):
    def test_buy_and_oversold_fire_sell_does_not(self) -> None:
        stock = {"ticker": "ANTM", "buy_below": 1300, "sell_above": 2000}
        snap = ps.Snapshot(price=1290.0, rsi=28.4, sma=1400.0, bar_date=date(2026, 10, 12))
        alerts = dict(ps.evaluate(stock, snap, 30))

        self.assertEqual(set(alerts), {"price_buy", "rsi_oversold"})
        self.assertTrue(alerts["price_buy"].startswith(
            "🚨 *BUY ALERT: ANTM*\nPrice dropped to *Rp 1,290*\n(Target: < Rp 1,300)"))
        self.assertTrue(alerts["rsi_oversold"].startswith(
            "📉 *TECHNICAL ALERT: ANTM OVERSOLD*\nRSI is currently at *28.4*."))

    def test_sell_with_missing_indicators(self) -> None:
        snap = ps.Snapshot(price=2150.0, rsi=None, sma=None, bar_date=date(2026, 10, 12))
        alerts = dict(ps.evaluate({"ticker": "MDKA", "sell_above": 2000}, snap, 30))

        self.assertEqual(list(alerts), ["price_sell"])
        self.assertIn("🎯 *SELL ALERT: MDKA*", alerts["price_sell"])

    def test_missing_or_invalid_thresholds_are_ignored(self) -> None:
        snap = ps.Snapshot(price=1290.0, rsi=50.0, sma=None, bar_date=date(2026, 10, 12))
        stock = {"ticker": "X", "buy_below": None, "sell_above": "bad"}
        self.assertEqual(ps.evaluate(stock, snap, 30), [])

    def test_stop_loss_fires_and_suppresses_buy_alert(self) -> None:
        stock = {"ticker": "ANTM", "buy_below": 2900, "sell_above": 3500, "stop_loss": 2750}
        snap = ps.Snapshot(price=2700.0, rsi=25.0, sma=3000.0, bar_date=date(2026, 10, 12))
        alerts = dict(ps.evaluate(stock, snap, 30))

        self.assertEqual(set(alerts), {"price_stop_loss", "rsi_oversold"})
        self.assertTrue(alerts["price_stop_loss"].startswith(
            "🛑 *STOP LOSS ALERT: ANTM*\nPrice dropped to *Rp 2,700*\n(Stop Loss: < Rp 2,750)"))

    def test_buy_fires_when_above_stop_loss_but_below_buy_below(self) -> None:
        stock = {"ticker": "ANTM", "buy_below": 2900, "sell_above": 3500, "stop_loss": 2750}
        snap = ps.Snapshot(price=2850.0, rsi=35.0, sma=3000.0, bar_date=date(2026, 10, 12))
        alerts = dict(ps.evaluate(stock, snap, 30))

        self.assertEqual(list(alerts), ["price_buy"])
        self.assertTrue(alerts["price_buy"].startswith(
            "🚨 *BUY ALERT: ANTM*\nPrice dropped to *Rp 2,850*\n(Target: < Rp 2,900)"))


class AlertLogTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "daily_alerts.log"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_stale_entries_cleared_and_today_kept(self) -> None:
        self.path.write_text(
            "2026-10-09|ANTM.JK_price_buy\n2026-10-12|BBRI.JK_rsi_oversold\n", encoding="utf-8")
        log = ps.AlertLog(self.path, "2026-10-12")

        self.assertTrue(log.has_fired("BBRI.JK_rsi_oversold"))
        self.assertFalse(log.has_fired("ANTM.JK_price_buy"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "2026-10-12|BBRI.JK_rsi_oversold\n")

    def test_recorded_alert_persists_until_next_day(self) -> None:
        ps.AlertLog(self.path, "2026-10-12").record("ANTM.JK_price_buy")

        self.assertTrue(ps.AlertLog(self.path, "2026-10-12").has_fired("ANTM.JK_price_buy"))
        self.assertEqual(ps.AlertLog(self.path, "2026-10-13").fired, set())
        self.assertEqual(self.path.read_text(encoding="utf-8"), "")


class MarketHoursTest(unittest.TestCase):
    CASES = [
        ((2026, 10, 12, 8, 59), False, "Mon pre-open"),
        ((2026, 10, 12, 9, 0), True, "Mon session I open"),
        ((2026, 10, 12, 12, 45), False, "Mon lunch break"),
        ((2026, 10, 12, 13, 30), True, "Mon session II"),
        ((2026, 10, 12, 15, 55), True, "Mon close + grace"),
        ((2026, 10, 12, 16, 15), False, "Mon after close"),
        ((2026, 10, 16, 11, 50), False, "Fri prayer break"),
        ((2026, 10, 16, 14, 0), True, "Fri session II"),
        ((2026, 10, 11, 10, 0), False, "Sunday"),
    ]

    def test_sessions(self) -> None:
        for parts, expected, label in self.CASES:
            with self.subTest(label):
                when = datetime(*parts, tzinfo=ps.WIB)
                self.assertIs(ps.market_status(when, grace_minutes=10)[0], expected)


class MorningBriefFormatTest(unittest.TestCase):
    HEADER = "🌅 *Morning Briefing - 12 Okt 2026*"

    def test_prompt_contains_formatted_data(self) -> None:
        now = datetime(2026, 10, 12, 8, 30, tzinfo=mb.WIB)
        macro = {
            "IHSG": {"close": 6093.76, "change_pct": 1.04, "as_of": "2026-10-09", "vol_ma": 2.1e8, "vol_ma_prev": 2.3e8, "vol_diff_pct": -7.5},
            "USD/IDR": {"close": 17875.0, "change_pct": 0.38, "as_of": "2026-10-09"},
            "Gold (Emas)": {"close": 2650.5, "change_pct": 0.8},
            "Crude Oil (WTI)": {"close": 71.5, "change_pct": -1.2},
        }
        flows = {"Perbankan": 12.5, "Energi & Batubara": -8.0}
        headlines = {
            "Kontan": [{"source": "Kontan", "title": "Rupiah melemah", "summary": "", "published": None}],
            "CNBC Indonesia": [],
        }
        prompt = mb.build_prompt(now, macro, flows, headlines)

        for needle in ("6,093.76", "Rp 17,875", "$2,650.50", "$71.50", "Morning Briefing - 12 Okt 2026",
                       "(feed tidak tersedia)", "WAKTU SAAT INI: Senin",
                       "*Makro & Pasar Global:*", "*Radar Berita & Katalis Utama Hari Ini:*",
                       "*Rotasi & Rekomendasi Sektor Unggulan:*", "/analyze <TICKER>"):
            self.assertIn(needle, prompt)

    def test_fallback_brief_structure(self) -> None:
        macro = {
            "IHSG": {"close": 6093.76, "change_pct": 1.04, "vol_diff_pct": -9.0},
            "USD/IDR": {"close": 17875.0, "change_pct": 0.38},
            "Gold (Emas)": {"close": 2650.0, "change_pct": 0.5},
            "Crude Oil (WTI)": {"close": 72.0, "change_pct": -0.8},
        }
        headlines = {"Kontan": [{"source": "Kontan", "title": "Rupiah Menguat", "summary": ""}]}
        flows = {"Perbankan": 12.5}
        fallback = mb.build_fallback_brief(self.HEADER, macro, headlines, flows=flows)

        self.assertIn("*Makro & Pasar Global:*", fallback)
        self.assertIn("*Radar Berita & Katalis Utama Hari Ini:*", fallback)
        self.assertIn("*Rotasi & Rekomendasi Sektor Unggulan:*", fallback)
        self.assertIn("/analyze <TICKER>", fallback)

    def test_model_output_sanitized_for_telegram(self) -> None:
        raw = "## Morning brief\n**Makro & Pasar:**\n• *IHSG:* 6.093 naik.\n\n\n* **BBRI:** laba solid\n```\nx\n```"
        out = mb.enforce_brief_format(raw, self.HEADER)

        self.assertTrue(out.startswith(self.HEADER))
        for forbidden in ("**", "```", "#", "\n\n\n"):
            self.assertNotIn(forbidden, out)
        self.assertIn("• *IHSG:*", out)
        self.assertIn("- *BBRI:*", out)

    def test_long_message_split_under_telegram_limit(self) -> None:
        chunks = mb.split_message("line\n" * 2000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= mb.TELEGRAM_MAX_LEN for c in chunks))


class GeminiFallbackTest(unittest.TestCase):
    """Mocked client - verifies model order, fast-fail on 4xx, and fallback."""

    @staticmethod
    def _client_error(code: int) -> Exception:
        return mb.genai_errors.ClientError(code, {"error": {"code": code, "message": "x", "status": "X"}})

    def _run(self, side_effects: list) -> tuple[str | Exception, list[str]]:
        from unittest import mock

        calls: list[str] = []
        effects = iter(side_effects)

        def fake_generate(model: str, contents: str, config: object) -> object:
            calls.append(model)
            effect = next(effects)
            if isinstance(effect, Exception):
                raise effect
            return mock.Mock(text=effect)

        client = mock.Mock()
        client.models.generate_content.side_effect = fake_generate
        with mock.patch.object(mb.genai, "Client", return_value=client), \
                mock.patch.object(mb.time, "sleep"):
            try:
                result: str | Exception = mb.synthesize_brief("prompt", "key")
            except Exception as exc:  # noqa: BLE001
                result = exc
        return result, calls

    def test_defaults(self) -> None:
        self.assertEqual(mb.gemini_models(), ["gemini-3.8-flash", "gemini-3.1-pro-preview"])

    def test_primary_success(self) -> None:
        result, calls = self._run(["brief"])
        self.assertEqual((result, calls), ("brief", ["gemini-3.8-flash"]))

    def test_404_skips_straight_to_fallback(self) -> None:
        result, calls = self._run([self._client_error(404), "pro brief"])
        self.assertEqual(result, "pro brief")
        self.assertEqual(calls, ["gemini-3.8-flash", "gemini-3.1-pro-preview"])

    def test_429_is_retried_on_same_model(self) -> None:
        result, calls = self._run([self._client_error(429), "brief"])
        self.assertEqual((result, calls), ("brief", ["gemini-3.8-flash", "gemini-3.8-flash"]))

    def test_all_models_failing_raises(self) -> None:
        result, calls = self._run([self._client_error(403), self._client_error(403)])
        self.assertIsInstance(result, RuntimeError)
        self.assertEqual(len(calls), 2)


class DeepAgentTest(unittest.TestCase):
    def test_normalize_ticker(self) -> None:
        self.assertEqual(da.normalize_ticker("bbri"), ("BBRI", "BBRI.JK"))
        self.assertEqual(da.normalize_ticker("ANTM.JK"), ("ANTM", "ANTM.JK"))
        self.assertEqual(da.normalize_ticker("  asii  "), ("ASII", "ASII.JK"))

    def test_pydantic_models(self) -> None:
        plan = da.TraderPlan(
            action="Akumulasi Beli",
            entry_range="Rp 3.000 - Rp 3.080",
            target_price=3500.0,
            stop_loss=2850.0,
            risk_reward_ratio=2.33,
            key_catalyst="Katalis dividen dan pertumbuhan kredit UMKM.",
            primary_risk="Risiko NPL jika daya beli melambat."
        )
        self.assertEqual(plan.action, "Akumulasi Beli")
        self.assertEqual(plan.target_price, 3500.0)
        self.assertEqual(plan.stop_loss, 2850.0)
        self.assertAlmostEqual(plan.risk_reward_ratio, 2.33)

        decision = da.PortfolioDecision(
            rating="OVERWEIGHT",
            time_horizon="2-6 minggu (Swing Trade)",
            allocation_advice="Alokasi 5-8% dari total portofolio saham",
            strategic_summary="Secara fundamental solid dengan valuasi atraktif."
        )
        self.assertEqual(decision.rating, "OVERWEIGHT")
        self.assertEqual(decision.time_horizon, "2-6 minggu (Swing Trade)")

    def test_graph_compilation(self) -> None:
        graph = da.build_deep_agent_graph()
        self.assertIsNotNone(graph)
        nodes = graph.nodes
        for expected in ("technical_analyst", "fundamentals_analyst", "news_analyst",
                         "volume_flow_analyst", "researcher_debate", "trader_proposal",
                         "portfolio_manager", "report_assembler"):
            self.assertIn(expected, nodes)

    def test_report_assembler_node(self) -> None:
        sample_state: da.AgentState = {
            "ticker": "BBRI",
            "symbol": "BBRI.JK",
            "company_name": "Bank Rakyat Indonesia",
            "sector": "Keuangan (Financials)",
            "technical": {
                "price": 3100.0,
                "change_1d": 1.5,
                "change_5d": 3.0,
                "sma_20": 3050.0,
                "sma_50": 3000.0,
                "sma_200": 3200.0,
                "ema_10": 3080.0,
                "rsi_14": 52.0,
                "atr_14": 80.0,
                "high_52w": 4000.0,
                "low_52w": 2800.0,
            },
            "fundamentals": {
                "pe": 8.5,
                "pbv": 1.8,
                "roe": 21.0,
                "der": 1.1,
                "div_yield": 5.5,
            },
            "volume_flow": {
                "vol_ratio_20d": 1.33,
                "flow_diagnosis": "🟢 Akumulasi Kuat",
            },
            "news": [
                {"title": "BBRI Catatkan Kinerja Positif Kuartal Ini", "link": "https://example.com"}
            ],
            "bull_thesis": "Argumen bull yang solid.",
            "bear_thesis": "Argumen bear yang waspada.",
            "trader_plan": {
                "action": "Akumulasi Beli",
                "entry_range": "Rp 3.050 - Rp 3.100",
                "target_price": 3300.0,
                "stop_loss": 2950.0,
                "risk_reward_ratio": 1.33,
                "key_catalyst": "Katalis laba solid",
                "primary_risk": "Risiko perlambatan kredit",
            },
            "portfolio_decision": {
                "rating": "OVERWEIGHT",
                "time_horizon": "2-6 minggu",
                "allocation_advice": "5-8%",
                "strategic_summary": "Disarankan akumulasi bertahap.",
            },
        }

        result = da.report_assembler_node(sample_state)
        report = result["report_text"]
        self.assertIn("DEEP-DIVE STOCK INTELLIGENCE: BBRI", report)
        self.assertIn("Snapshot Harga:", report)
        self.assertIn("Katalis Berita Terkini:", report)
        self.assertIn("Bull Researcher Thesis", report)
        self.assertIn("Bear Researcher Thesis", report)
        self.assertIn("Rencana Perdagangan (Trader Plan):", report)
        self.assertIn("Keputusan Akhir Portfolio Manager:", report)

    def test_split_message(self) -> None:
        long_text = "Baris uji coba telegram.\n" * 300
        chunks = da.split_message(long_text, limit=1000)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 1000 for c in chunks))


if __name__ == "__main__":
    unittest.main()
