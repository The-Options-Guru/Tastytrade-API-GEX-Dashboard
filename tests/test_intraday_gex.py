import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from tests.test_snapshot_history import CONFIG, DATA, START, fixture_row
from utils.intraday_gex import (
    VIEWS, build_intraday_gex_chart, gex_changes, make_gex_capture, top_gex_levels,
)
from utils.snapshot_history import HistoryStore


def capture(at_ms=START, data=None, prices=None, **config):
    return make_gex_capture(
        dict(CONFIG, **config), "261022", date(2026, 10, 22),
        prices if prices is not None else dict(last=100, last_time=at_ms),
        DATA if data is None else data, list(DATA), at_ms, "Test fixture",
    )


class IntradayGexTests(unittest.TestCase):
    def test_formula_missing_contract_and_zero_are_distinct(self):
        complete = capture()
        values = {row["strike"]: row["net_oi_gex"] for row in complete["rows"]}
        self.assertEqual(values, {90: -29000, 100: 0, 110: 29000})
        incomplete = dict(DATA)
        del incomplete[".SPY261022P90"]
        rows = {row["strike"]: row for row in capture(data=incomplete)["rows"]}
        self.assertIsNone(rows[90]["net_oi_gex"])
        self.assertEqual(rows[100]["net_oi_gex"], 0)
        json.dumps(capture(data=incomplete), allow_nan=False)

    def test_stale_missing_future_and_wrong_day_price_are_gaps(self):
        for prices in (
            {}, dict(last=100), dict(last=100, last_time=START - 120001),
            dict(last=100, last_time=START + 1), dict(last=100, last_time=START - 86400000),
            dict(price=100, last_time=START), dict(last=-1, last_time=START),
        ):
            with self.subTest(prices=prices):
                result = capture(prices=prices)
                self.assertIsNone(result["spot"])
                self.assertTrue(all(row["net_oi_gex"] is None for row in result["rows"]))

    def test_changes_align_strikes_and_do_not_fill_missing_or_cross_scopes(self):
        before = capture()
        data = {symbol: dict(values) for symbol, values in DATA.items()}
        data[".SPY261022C110"]["oi"] = 40
        del data[".SPY261022P90"]
        latest = capture(START + 60000, data)
        delta = gex_changes(latest, before).set_index("strike")
        self.assertEqual(delta.loc[110, "Change"], 10000)
        self.assertEqual(delta.loc[100, "Change"], 0)
        self.assertTrue(delta.loc[90, "Change"] != delta.loc[90, "Change"])
        latest["rows"].append(dict(strike=120, net_oi_gex=900, call_oi_gex=900, put_oi_gex=0))
        delta = gex_changes(latest, before).set_index("strike")
        self.assertIn("Unavailable", delta.loc[120, "Status"])
        for change in (dict(scope="other"), dict(session="2026-10-23"), dict(at_ms=START)):
            with self.assertRaises(ValueError):
                gex_changes(dict(latest, **change), before)

    def test_latest_levels_replace_older_taller_levels_and_charts_preserve_gaps(self):
        old = capture()
        data = {symbol: dict(values) for symbol, values in DATA.items()}
        data[".SPY261022C110"]["oi"] = 1
        data[".SPY261022C100"]["oi"] = 12
        latest = capture(START + 300000, data)
        positive, negative = top_gex_levels(latest)
        self.assertEqual(positive.strike.tolist(), [100])
        self.assertEqual(negative.strike.tolist(), [90])
        chart = build_intraday_gex_chart([old, latest], "Levels")
        self.assertEqual([shape.y0 for shape in chart.layout.shapes], [100, 90])
        self.assertIn(None, chart.data[0].y)
        heatmap = build_intraday_gex_chart([old, latest], "Throughout day").data[0]
        self.assertEqual(len(heatmap.x), 6)
        self.assertTrue(all(row[1] is None for row in heatmap.z))
        self.assertEqual(list(build_intraday_gex_chart([old], "Strikes").data[0].y), [-29000, 0, 29000])
        self.assertEqual(list(build_intraday_gex_chart([old, latest], "Changes", old).data[0].y), [0, 2000, -29000])
        with self.assertRaises(ValueError):
            build_intraday_gex_chart([old], "Changes")

    def test_new_storage_keeps_every_poll_and_does_not_change_wall_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with HistoryStore(path) as store:
                store.observe(fixture_row(), START, START)
                original = store.db.execute("SELECT payload FROM samples").fetchone()[0]
                first, second = capture(), capture(START + 60000)
                self.assertTrue(store.save_gex_capture(first))
                self.assertFalse(store.save_gex_capture(first))
                self.assertTrue(store.save_gex_capture(second))
                self.assertEqual(store.db.execute("SELECT payload FROM samples").fetchone()[0], original)
                self.assertEqual(store.gex_sessions(CONFIG, "261022"), ["2026-10-22"])
                for config, expiry, session in (
                    (dict(CONFIG, symbol="QQQ"), "261022", "2026-10-22"),
                    (dict(CONFIG, strikes_up=5), "261022", "2026-10-22"),
                    (CONFIG, "261023", "2026-10-22"), (CONFIG, "261022", "2026-10-23"),
                ):
                    self.assertEqual(store.gex_rows(config, expiry, session), [])
            with HistoryStore(path) as store:
                self.assertEqual(store.gex_rows(CONFIG, "261022", "2026-10-22"), [first, second])

    def test_dropdown_all_views_baseline_and_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with HistoryStore(path) as store:
                for offset in (0, 60000, 120000):
                    store.save_gex_capture(capture(START + offset))
            script = """
from datetime import date
from simple_dashboard import render_intraday_gex
from utils.snapshot_history import DEFAULT_CONFIG
render_intraday_gex(dict(DEFAULT_CONFIG, symbol="SPY", root="SPY"), "261022", date(2026, 10, 22))
"""
            with patch("utils.snapshot_history.HISTORY_DB", path):
                app = AppTest.from_string(script).run(timeout=30)
                for view in VIEWS:
                    app.selectbox(key="daily_gex_view").set_value(view).run(timeout=30)
                    self.assertFalse(app.exception)
                    self.assertFalse(app.error)
                    self.assertEqual(len(app.get("plotly_chart")), 1)
                self.assertEqual(app.selectbox(key="daily_gex_comparison").value, "Previous recorded capture")
                app.selectbox(key="daily_gex_comparison").set_value("Choose capture").run(timeout=30)
                self.assertTrue(any(item.label == "Baseline capture (ET)" for item in app.selectbox))
                empty = AppTest.from_string(script.replace('"261022"', '"261023"')).run(timeout=30)
                self.assertFalse(empty.exception)
                self.assertEqual(len(empty.get("plotly_chart")), 0)
                self.assertTrue(any("No per-strike" in item.value for item in empty.info))


if __name__ == "__main__":
    unittest.main()
