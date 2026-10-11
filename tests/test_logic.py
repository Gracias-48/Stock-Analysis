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
            "IHSG": {"close": 6093.76, "change_pct": 1.04, "as_of": "2026-10-09"},
            "USD/IDR": {"close": 17875.0, "change_pct": 0.38, "as_of": "2026-10-09"},
        }
        stocks = [{
            "ticker": "BBRI", "sector": "Banking", "name": "BRI", "close": 3100.0,
            "change_pct": 2.31, "buy_below": 2850, "sell_above": 3450,
            "vol_ma": 2.1e8, "vol_ma_prev": 2.3e8, "vol_diff_pct": -7.5,
            "rsi": 45.4, "sma": 3189.0, "pe": 7.6, "pbv": 1.45,
        }]
        headlines = {
            "Kontan": [{"source": "Kontan", "title": "Rupiah melemah", "summary": "", "published": None}],
            "CNBC Indonesia": [],
        }
        prompt = mb.build_prompt(now, macro, stocks, mb.sector_volume_flows(stocks), headlines, [])

        for needle in ("6,093.76", "Rp 17,875", "Rp 3,100", "Morning Briefing - 12 Okt 2026",
                       "(feed tidak tersedia)", "WAKTU SAAT INI: Senin",
                       "Technical -> RSI: 45.4, SMA20: Rp 3,189",
                       "Fundamental -> P/E: 7.6x, PBV: 1.45x"):
            self.assertIn(needle, prompt)

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


if __name__ == "__main__":
    unittest.main()
