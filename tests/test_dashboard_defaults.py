import unittest
import re
from datetime import date, datetime
from html import unescape
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest
from utils.gex_calculator import GEXCalculator
from utils.market_hours import MarketState


class DashboardDefaultTests(unittest.TestCase):
    def test_top_sentiment_follows_selected_symbol_without_reusing_other_symbol_price(self):
        now = datetime.now(ZoneInfo("America/New_York"))
        day_id = (now.date() - date(1970, 1, 1)).days
        app = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "simple_dashboard.py"))
        app.session_state["auto_refresh"] = False
        app.session_state["symbol"] = "QQQ"
        app.session_state["market_prices"] = dict(
            last=101, open=100, day_id=day_id, last_day_id=day_id, last_time=now.timestamp() * 1000,
        )
        with patch("utils.market_hours.market_state", return_value=MarketState(False, "Test closed", now.date())), \
             patch("utils.auth.ensure_streamer_token", side_effect=AssertionError("No broker calls")):
            app.run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].label, "SPX market sentiment")
            self.assertEqual(app.metric[0].value, "Unavailable")
            self.assertEqual(app.metric[1].value, "Unavailable")
            next(item for item in app.selectbox if item.label == "Underlying").set_value("QQQ").run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.metric[0].label, "QQQ market sentiment")
            self.assertEqual(app.metric[0].value, "Bullish")

    def test_side_wall_and_signed_net_peak_columns_coexist(self):
        app = AppTest.from_string("""
from simple_dashboard import render_market_summary
from utils.exposure_profile import build_exposure_profile, get_wall_levels
data = {
    f".SPY261022{side}{strike}": dict(gamma=0.1, oi=oi, volume=0)
    for strike, calls, puts in ((100, 20, 30), (110, 10, 1))
    for side, oi in (("C", calls), ("P", puts))
}
profile = build_exposure_profile(data, 100)
render_market_summary(profile, get_wall_levels(profile, 100), 100, None, False, {"last": 100}, data)
""").run(timeout=30)
        self.assertFalse(app.exception)
        html = app.get("html")[0].proto.body
        headings = re.findall(r"<th\b[^>]*>(.*?)</th>", html, re.DOTALL)
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", html, re.DOTALL)
        overview = {unescape(key): unescape(value) for key, value in zip(headings, cells)}
        self.assertEqual(overview["Call wall strike"], "$100.00")
        self.assertEqual(overview["Call wall strength"], "$20.00K / 1% move")
        self.assertEqual(overview["Positive net GEX strike"], "$110.00")
        self.assertEqual(overview["Positive net GEX value"], "$9.00K / 1% move")
        self.assertEqual(overview["Negative net GEX strike"], "$100.00")
        self.assertEqual(overview["Negative net GEX value"], "$-10.00K / 1% move")
        self.assertTrue(any("not verified Unusual Whales" in item.value for item in app.caption))

    def test_populated_heatmap_and_volume_toggle_without_live_api_access(self):
        dashboard = Path(__file__).resolve().parent.parent / "simple_dashboard.py"
        data = {
            ".SPXW261022C6000": {"gamma": 0.01, "oi": 20, "volume": 40},
            ".SPXW261022P6000": {"gamma": 0.01, "oi": 30, "volume": 10},
            ".SPXW261022C6005": {"gamma": 0.01, "oi": 10, "volume": 20},
            ".SPXW261022P6005": {"gamma": 0.01, "oi": 10, "volume": 10},
        }
        calc = GEXCalculator(spot_price=6000)
        for symbol, values in data.items():
            calc.update_gamma(symbol, values["gamma"], values["oi"])
        app = AppTest.from_file(str(dashboard))
        for name, value in {
            "auto_refresh": False, "data_fetched": True, "option_data": data,
            "gex_calculator": calc, "underlying_price": 6000,
            "symbol": "SPX", "expiration": "261022", "option_count": 4,
            "max_pain": {"strikes": [6000, 6005], "observed": 4, "expected": 4, "payout": 5000},
            "market_prices": {"open": 6010, "high": 6020, "low": 5990, "last": 6000,
                              "comparable": True, "day_id": 20748, "last_time": 1792701108494},
        }.items():
            app.session_state[name] = value
        with patch("utils.auth.ensure_streamer_token", side_effect=AssertionError("No broker calls")), \
             patch("utils.market_hours.market_state", return_value=MarketState(False, "Test closed", date(2026, 10, 22))):
            app.run(timeout=30)
            self.assertFalse(app.exception)
            metrics = {metric.label: metric.value for metric in app.metric}
            self.assertEqual(metrics["Call wall (OI gamma)"], "$6,000.00")
            self.assertEqual(metrics["Put wall (OI gamma)"], "$6,000.00")
            self.assertEqual(metrics["Directionalized GEX"], "Unavailable")
            html = app.get("html")[0].proto.body
            headings = re.findall(r"<th\b[^>]*>(.*?)</th>", html, re.DOTALL)
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", html, re.DOTALL)
            self.assertEqual(len(headings), len(cells))
            self.assertGreater(len(headings), 15)
            overview = {unescape(key): unescape(value) for key, value in zip(headings, cells)}
            self.assertEqual(overview["Current price"], "$6,000.00")
            self.assertEqual(overview["Call wall strike"], "$6,000.00")
            self.assertEqual(overview["Call wall strength"], "$7.20M / 1% move")
            self.assertEqual(overview["Call wall OI"], "20 contracts")
            self.assertEqual(overview["Put wall strike"], "$6,000.00")
            self.assertEqual(overview["Put wall strength"], "$10.80M / 1% move")
            self.assertEqual(overview["Put wall OI"], "30 contracts")
            self.assertEqual(overview["Positive net GEX strike"], "Unavailable")
            self.assertEqual(overview["Positive net GEX value"], "Unavailable")
            self.assertEqual(overview["Negative net GEX strike"], "$6,000.00")
            self.assertEqual(overview["Negative net GEX value"], "$-3.60M / 1% move")
            self.assertEqual(overview["Previous official close"], "Unavailable")
            self.assertEqual(overview["P/C ratio for OI"], "1.3333")
            self.assertEqual(overview["Max pain (full expiry)"], "$6,000.00 - $6,005.00 (tied minima)")
            self.assertIn("overflow-x:auto", html)
            self.assertIn("#36cba5", html)
            self.assertIn("#f45b76", html)
            subheaders = [item.value for item in app.subheader]
            self.assertLess(subheaders.index("Market summary history"), subheaders.index("Intraday Flow & GEX"))
            summary = app.dataframe[0].value.set_index("Metric")
            self.assertEqual(summary.loc["Call wall", "Strike / status"], "$6,000.00")
            self.assertEqual(summary.loc["Max pain (full expiry)", "Strike / status"], "$6,000.00 - $6,005.00 (tied minima)")
            self.assertEqual(summary.loc["Possible close direction", "Strike / status"], "Neutral (unvalidated)")
            self.assertIn("4/4 listed", summary.loc["Max pain (full expiry)", "Strength / basis"])
            self.assertIn("66.7%", summary.loc["Call wall", "Strength / basis"])
            self.assertEqual(summary.loc["Call wall OI", "Strike / status"], "20 contracts")
            self.assertEqual(summary.loc["Put wall OI", "Strike / status"], "30 contracts")
            self.assertEqual(summary.loc["Absolute GEX peak", "Strike / status"], "$6,000.00")
            self.assertIn("-3.60M", summary.loc["Net GEX peak", "Strength / basis"])
            self.assertEqual(summary.loc["Positive net GEX peak", "Strike / status"], "Unavailable")
            self.assertIn("No positive", summary.loc["Positive net GEX peak", "Strength / basis"])
            self.assertIn("-3.60M", summary.loc["Negative net GEX peak", "Strength / basis"])
            self.assertEqual(summary.loc["P/C ratio for OI", "Strike / status"], "1.3333")
            self.assertEqual(summary.loc["P/C ratio for Volume", "Strike / status"], "0.3333")
            self.assertEqual(summary.loc["Open price", "Strike / status"], "$6,010.00")
            self.assertEqual(summary.loc["High price", "Strike / status"], "$6,020.00")
            self.assertEqual(summary.loc["Low price", "Strike / status"], "$5,990.00")
            self.assertEqual(summary.loc["Last known close / latest price", "Strike / status"], "$6,000.00")
            self.assertIn("Down -10.00", summary.loc["Market direction from open", "Strike / status"])
            self.assertEqual(summary.loc["Price vs call / put wall", "Strike / status"], "Nearest: Equidistant")
            self.assertTrue(any("trade-side history" in item.value for item in app.info))
            charts = app.get("plotly_chart")
            self.assertGreaterEqual(len(charts), 4)
            self.assertEqual(app.selectbox(key="wall_heatmap_strikes").value, "25")
            app.selectbox(key="wall_heatmap_strikes").set_value("All").run(timeout=30)
            self.assertFalse(app.exception)
            app.radio(key="wall_heatmap_measure").set_value("Contract volume").run(timeout=30)
            self.assertFalse(app.exception)
            self.assertEqual(app.radio(key="wall_heatmap_measure").value, "Contract volume")

    def test_defaults_without_live_api_access(self):
        dashboard = Path(__file__).resolve().parent.parent / "simple_dashboard.py"
        with patch(
            "utils.auth.ensure_streamer_token",
            side_effect=ValueError("Test: live authentication disabled"),
        ), patch("utils.market_hours.market_state", return_value=MarketState(True, "Test session", datetime.now(ZoneInfo("America/New_York")).date())):
            app = AppTest.from_file(str(dashboard)).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertTrue(app.checkbox[0].value)
        self.assertEqual(app.slider[0].value, 60)
        self.assertEqual([field.value for field in app.number_input], [50, 50])
        self.assertEqual(
            app.date_input[0].value,
            datetime.now(ZoneInfo("America/New_York")).date(),
        )
        self.assertEqual(app.date_input[0].proto.format, "MM/DD/YYYY")
        self.assertTrue(any(
            "Test: live authentication disabled" in error.value
            for error in app.error
        ))

    def test_closed_auto_refresh_does_not_call_broker(self):
        dashboard = Path(__file__).resolve().parent.parent / "simple_dashboard.py"
        with patch("utils.auth.ensure_streamer_token", side_effect=AssertionError("No closed-market auth")) as auth, \
             patch("utils.option_chain.fetch_expiry_chain", side_effect=AssertionError("No closed-market chain")) as chain, \
             patch("utils.market_hours.market_state", return_value=MarketState(False, "Test closed", date(2026, 10, 23))):
            app = AppTest.from_file(str(dashboard)).run(timeout=30)
            self.assertFalse(app.exception)
            self.assertTrue(app.checkbox[0].value)
            self.assertEqual(app.date_input[0].value, date(2026, 10, 23))
            self.assertTrue(any("Auto-fetch paused" in item.value for item in app.info))
            auth.assert_not_called()
            chain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
