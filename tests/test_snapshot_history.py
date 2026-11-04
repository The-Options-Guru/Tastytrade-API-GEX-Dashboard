import csv
import io
import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

import pandas as pd
import streamlit as st
from streamlit.testing.v1 import AppTest

import history_recorder
from utils.flow_store import NEW_YORK
from utils.gex_calculator import parse_option_symbol
from utils.market_hours import MarketState
from utils.intraday_gex import make_gex_capture
from utils.snapshot_feed import connect_websocket, get_underlying_snapshot
from utils.snapshot_history import (
    COLUMNS, DEFAULT_CONFIG, HistoryStore, capture_reason, confirmed_previous_close, crossing_history, csv_text,
    history_style, make_row, snapshot_metrics, validate_config, wall_strength_history,
)


START = int(datetime(2026, 10, 22, 10, tzinfo=NEW_YORK).timestamp() * 1000)
DATA = {
    f".SPY261022{side}{strike}": dict(gamma=0.1, oi=oi, volume=volume)
    for side, strike, oi, volume in (
        ("C", 90, 1, 10), ("P", 90, 30, 5), ("C", 100, 10, 20),
        ("P", 100, 10, 15), ("C", 110, 30, 30), ("P", 110, 1, 10),
    )
}
CONFIG = dict(DEFAULT_CONFIG, symbol="SPY", root="SPY")


def fixture_row(at_ms=START, spot=100, session=date(2026, 10, 22), **changes):
    prices = dict(
        open=99, high=112, low=88, last=spot, last_time=at_ms, day_id=20748,
        last_day_id=20748, prev_day_id=20747, prev_close=98, prev_close_type="FINAL",
    )
    row = make_row(CONFIG, "261022", session, prices, DATA, list(DATA), at_ms)
    row.update(changes)
    return row


class MetricTests(unittest.TestCase):
    def test_independent_expected_metrics_and_tie_breaks(self):
        metrics = snapshot_metrics(DATA, 100)
        self.assertEqual(metrics["CALL WALL STRIKE"], 110)
        self.assertEqual(metrics["PUT WALL STRIKE"], 90)
        self.assertEqual(metrics["CALL WALL OI"], 30)
        self.assertEqual(metrics["PUT WALL OI"], 30)
        self.assertEqual(metrics["CALL WALL STRENGTH"], 30000)
        self.assertEqual((metrics["ABS GEX STRIKE"], metrics["ABS GEX VALUE"]), (90, 31000))
        self.assertEqual((metrics["NET GEX STRIKE"], metrics["NET GEX VALUE"]), (90, -29000))
        self.assertEqual(metrics["P/C RATIO FOR OI"], 1)
        self.assertEqual(metrics["P/C RATIO FOR VOLUME"], 0.5)

    def test_net_peaks_differ_from_side_walls_on_identical_inputs(self):
        data = {
            f".SPY261022{side}{strike}": dict(gamma=0.1, oi=oi, volume=0)
            for strike, calls, puts in ((90, 29, 30), (95, 1, 20), (100, 10, 10), (105, 20, 1), (110, 30, 29))
            for side, oi in (("C", calls), ("P", puts))
        }
        result = snapshot_metrics(data, 100)
        self.assertEqual((result["CALL WALL STRIKE"], result["CALL WALL STRENGTH"]), (110, 30000))
        self.assertEqual((result["PUT WALL STRIKE"], result["PUT WALL STRENGTH"]), (90, 30000))
        self.assertEqual((result["POSITIVE NET GEX STRIKE"], result["POSITIVE NET GEX VALUE"]), (105, 19000))
        self.assertEqual((result["NEGATIVE NET GEX STRIKE"], result["NEGATIVE NET GEX VALUE"]), (95, -19000))
        self.assertEqual((result["NET GEX STRIKE"], result["NET GEX VALUE"]), (95, -19000))
        self.assertEqual((result["ABS GEX STRIKE"], result["ABS GEX VALUE"]), (90, 59000))

    def test_net_peak_ties_favor_nearest_price_then_lower_strike(self):
        for calls, puts, label in ((20, 1, "POSITIVE"), (1, 20, "NEGATIVE")):
            data = {
                f".SPY261022{side}{strike}": dict(gamma=0.1, oi=oi)
                for strike in (90, 95, 105, 110)
                for side, oi in (("C", calls), ("P", puts))
            }
            self.assertEqual(snapshot_metrics(data, 100)[f"{label} NET GEX STRIKE"], 95)
            self.assertEqual(snapshot_metrics(data, 106)[f"{label} NET GEX STRIKE"], 105)
            self.assertEqual(snapshot_metrics(data, 94)[f"{label} NET GEX STRIKE"], 95)

    def test_missing_net_sign_is_unavailable_not_zero(self):
        for calls, puts, absent in ((1, 20, ("POSITIVE",)), (20, 1, ("NEGATIVE",)), (1, 1, ("POSITIVE", "NEGATIVE"))):
            data = {
                f".SPY261022{side}100": dict(gamma=0.1, oi=oi)
                for side, oi in (("C", calls), ("P", puts))
            }
            result = snapshot_metrics(data, 100)
            for label in absent:
                self.assertIsNone(result[f"{label} NET GEX STRIKE"])
                self.assertIsNone(result[f"{label} NET GEX VALUE"])
            if calls != puts:
                label = "POSITIVE" if calls > puts else "NEGATIVE"
                self.assertEqual(result[f"{label} NET GEX STRIKE"], 100)
                self.assertEqual(result[f"{label} NET GEX VALUE"], (calls - puts) * 1000)

    def test_incomplete_net_inputs_block_both_peaks(self):
        for field in ("oi", "gamma"):
            for bad in (None, -1, True, float("nan"), float("inf")):
                with self.subTest(field=field, bad=bad):
                    data = {symbol: dict(values) for symbol, values in DATA.items()}
                    data[".SPY261022C100"][field] = bad
                    result = snapshot_metrics(data, 100)
                    for sign in ("POSITIVE", "NEGATIVE"):
                        for suffix in ("STRIKE", "VALUE"):
                            self.assertIsNone(result[f"{sign} NET GEX {suffix}"])
        for data in ({}, {".SPY261022C100": DATA[".SPY261022C100"]}):
            result = snapshot_metrics(data, 100)
            self.assertIsNone(result["POSITIVE NET GEX VALUE"])
            self.assertIsNone(result["NEGATIVE NET GEX VALUE"])

    def test_missing_contract_not_a_zero_or_a_provisional_wall_change(self):
        incomplete = dict(DATA)
        del incomplete[".SPY261022P90"]
        result = snapshot_metrics(incomplete, 100, list(DATA))
        self.assertEqual(result["OI COVERAGE"], "5/6")
        for field in (
            "PUT WALL STRIKE", "PUT WALL OI", "ABS GEX VALUE", "NET GEX VALUE", "P/C RATIO FOR OI",
            "POSITIVE NET GEX VALUE", "NEGATIVE NET GEX VALUE",
        ):
            self.assertIsNone(result[field])
        self.assertEqual(result["CALL WALL STRIKE"], 110)

    def test_zero_denominator_and_missing_volume_are_unavailable(self):
        for value in (0, None, -1, True, float("nan")):
            data = {symbol: dict(values, volume=value) if parse_option_symbol(symbol)["type"] == "C" else values for symbol, values in DATA.items()}
            self.assertIsNone(snapshot_metrics(data, 100)["P/C RATIO FOR VOLUME"])
        data = {symbol: dict(values, volume=0) if parse_option_symbol(symbol)["type"] == "P" else values for symbol, values in DATA.items()}
        self.assertEqual(snapshot_metrics(data, 100)["P/C RATIO FOR VOLUME"], 0)

    def test_official_close_requires_previous_day_and_final_type(self):
        valid = dict(prev_close=98, prev_close_type="FINAL", prev_day_id=20747, day_id=20748)
        self.assertEqual(confirmed_previous_close(valid), 98)
        for change in (
            dict(prev_close_type="REGULAR"), dict(prev_close_type="PRELIMINARY"),
            dict(prev_day_id=20748), dict(day_id=None), dict(prev_close=float("nan")),
        ):
            self.assertIsNone(confirmed_previous_close(dict(valid, **change)))

    def test_snapshot_previous_close_is_not_current_or_midpoint(self):
        ws = Mock()
        ws.recv.return_value = json.dumps(dict(type="FEED_DATA", data=[
            dict(eventSymbol="SPY", eventType="Trade", price=100, dayId=20748, time=START),
            dict(eventSymbol="SPY", eventType="Summary", dayId=20748, dayOpenPrice=99,
                 dayHighPrice=110, dayLowPrice=90, prevDayId=20747,
                 prevDayClosePrice=98, prevDayClosePriceType="FINAL"),
        ]))
        prices = get_underlying_snapshot(ws, "SPY")
        self.assertEqual(prices["last"], 100)
        self.assertEqual(confirmed_previous_close(prices), 98)

    def test_config_validation(self):
        self.assertEqual(validate_config(DEFAULT_CONFIG), DEFAULT_CONFIG)
        for change in (dict(symbol="=cmd"), dict(strikes_up=51), dict(expiry="261099"), dict(increment=0), dict(enabled=1)):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_config(dict(DEFAULT_CONFIG, **change))


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "history.sqlite3"
        self.store = HistoryStore(self.path)

    def tearDown(self):
        self.store.__exit__()
        self.directory.cleanup()

    def observe(self, seconds=0, spot=100, **changes):
        at_ms = START + seconds * 1000
        return self.store.observe(fixture_row(at_ms, spot, **changes), at_ms, at_ms)

    def test_15_minute_strength_captures_and_either_wall_strike_change(self):
        self.assertTrue(self.observe())
        self.assertFalse(self.observe(60))
        self.assertTrue(self.observe(120, **{"CALL WALL STRIKE": 115}))
        self.assertFalse(self.observe(120, **{"CALL WALL STRIKE": 115}))
        self.assertFalse(self.observe(899, **{"CALL WALL STRIKE": 115}))
        self.assertEqual(len(self.store.rows()), 2)
        self.assertTrue(self.observe(900, **{"CALL WALL STRIKE": 115}))
        self.assertIn("15-minute", self.store.rows()[-1]["REMARKS"])
        self.assertTrue(self.observe(960, **{"CALL WALL STRIKE": 115, "PUT WALL STRIKE": 85}))
        self.assertFalse(self.observe(1799, **{
            "CALL WALL STRIKE": 115, "PUT WALL STRIKE": 85,
            "CALL WALL STRENGTH": 60000, "PUT WALL STRENGTH": 90000,
        }))
        self.assertTrue(self.observe(1800, **{
            "CALL WALL STRIKE": 115, "PUT WALL STRIKE": 85,
            "CALL WALL STRENGTH": 60000, "PUT WALL STRENGTH": 90000,
        }))
        rows = self.store.rows()
        self.assertEqual(len(rows), 5)
        self.assertIn("Put wall changed", rows[-2]["REMARKS"])
        self.assertEqual(rows[-1]["CALL WALL STRENGTH"], 60000)
        self.assertEqual(rows[-1]["PUT WALL STRENGTH"], 90000)
        self.assertIn("15-minute", rows[-1]["REMARKS"])

    def test_restart_preserves_sequence_and_no_backfill(self):
        self.observe()
        self.store.__exit__()
        self.store = HistoryStore(self.path)
        self.observe(3600)
        rows = self.store.rows()
        self.assertEqual([row["SLNO"] for row in rows], [1, 2])
        self.assertIn("no backfill", rows[-1]["REMARKS"])

    def test_stationary_price_and_moving_wall_do_not_create_crossing(self):
        self.observe(0, 100, **{"CALL WALL STRIKE": 105})
        self.observe(60, 100, **{"CALL WALL STRIKE": 95})
        rows = self.store.rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row["CALL WALL CROSSED"] for row in rows))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM crossings").fetchone()[0], 0)

    def test_up_down_call_put_and_old_wall_marks_persist_without_extra_snapshot(self):
        self.observe(0, 100)
        self.assertTrue(self.observe(60, 111))
        self.assertTrue(self.observe(120, 89))
        rows = self.store.rows()
        self.assertEqual(len(rows), 1)
        self.assertIn("UP", rows[0]["CALL WALL CROSSED"])
        self.assertIn("DOWN", rows[0]["CALL WALL CROSSED"])
        self.assertIn("DOWN", rows[0]["PUT WALL CROSSED"])
        self.store.export()
        saved = list(csv.DictReader(io.StringIO(self.path.with_suffix(".csv").read_text(encoding="utf-8-sig"))))
        self.assertIn("sampled", saved[0]["CALL WALL CROSSED"])

    def test_old_wall_still_tracked_after_wall_moves(self):
        self.observe(0, 100, **{"CALL WALL STRIKE": 105})
        self.observe(60, 101, **{"CALL WALL STRIKE": 110})
        self.observe(120, 106, **{"CALL WALL STRIKE": 110})
        rows = self.store.rows()
        self.assertIn("UP", rows[0]["CALL WALL CROSSED"])
        self.assertEqual(rows[1]["CALL WALL CROSSED"], "")

    def test_touch_is_not_crossing_and_repeated_touch_keeps_baseline(self):
        self.observe(0, 109)
        self.observe(60, 110)
        self.observe(120, 110)
        self.assertEqual(self.store.rows()[0]["CALL WALL CROSSED"], "")
        self.observe(180, 111)
        self.assertIn("UP", self.store.rows()[0]["CALL WALL CROSSED"])

    def test_sessions_provider_days_and_configuration_are_isolated(self):
        self.observe(0, 100)
        self.observe(60, 111, **{"PRICE SESSION DATE": "10/23/2026"})
        self.assertEqual(self.store.rows()[0]["CALL WALL CROSSED"], "")
        self.observe(900, 89, **{"SESSION DATE": "10/23/2026"})
        self.assertEqual(self.store.rows()[0]["PUT WALL CROSSED"], "")
        self.observe(960, 120, **{"SESSION DATE": "10/23/2026", "STRIKES ABOVE": 25})
        self.assertEqual(len(self.store.rows()), 3)

    def test_missing_wall_does_not_generate_spurious_change(self):
        self.observe()
        self.assertFalse(self.observe(60, **{"CALL WALL STRIKE": None, "PUT WALL STRIKE": None}))
        self.assertFalse(self.observe(120))
        self.assertEqual(len(self.store.rows()), 1)

    def test_unavailable_or_repeated_trade_timestamps_do_not_prove_crossing(self):
        self.observe(0, 100)
        self.store.observe(fixture_row(START + 60000, 111), START + 60000, START)
        self.assertEqual(self.store.rows()[0]["CALL WALL CROSSED"], "")
        self.store.observe(fixture_row(START + 120000, 89), START + 120000, None)
        self.assertEqual(self.store.rows()[0]["PUT WALL CROSSED"], "")

    def test_csv_schema_order_numbers_blanks_and_colors(self):
        self.observe(0, 100, **{"CLOSE PRICE": None})
        self.observe(60, 111)
        rows = self.store.rows()
        text = csv_text(rows)
        parsed = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(list(parsed[0]), COLUMNS)
        self.assertEqual(parsed[0]["CLOSE PRICE"], "")
        self.assertEqual(float(parsed[0]["NET GEX VALUE"]), -29000)
        styles = history_style(pd.DataFrame(rows))
        self.assertIn("#36cba5", styles.iloc[0]["CALL WALL STRIKE"])
        self.assertIn("bold", styles.iloc[0]["CALL WALL STRIKE"])
        self.assertIn("#f45b76", styles.iloc[0]["PUT WALL STRIKE"])
        self.assertIn("#36cba5", styles.iloc[0]["POSITIVE NET GEX STRIKE"])
        self.assertIn("#f45b76", styles.iloc[0]["NEGATIVE NET GEX VALUE"])
        self.store.export()
        self.assertEqual(self.path.with_suffix(".csv").read_text(encoding="utf-8-sig"), text.replace("\r\n", "\n"))

    def test_new_net_columns_append_without_reinterpreting_historical_payloads(self):
        columns = [
            "POSITIVE NET GEX STRIKE", "POSITIVE NET GEX VALUE",
            "NEGATIVE NET GEX STRIKE", "NEGATIVE NET GEX VALUE",
        ]
        old = fixture_row()
        for column in columns:
            old.pop(column)
        self.store.observe(old, START, START)
        payload = self.store.db.execute("SELECT payload FROM samples WHERE id=1").fetchone()[0]
        self.observe(900)
        self.store.export()
        with self.path.with_suffix(".csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
            self.assertEqual(reader.fieldnames[40], "OBSERVATION GAP SECONDS")
            self.assertEqual(reader.fieldnames[41:], columns)
        for column in columns:
            self.assertEqual(rows[0][column], "")
        self.assertEqual(float(rows[1]["POSITIVE NET GEX VALUE"]), 29000)
        self.assertEqual(float(rows[1]["NEGATIVE NET GEX VALUE"]), -29000)
        self.assertEqual(rows[0]["CALL WALL STRIKE"], rows[1]["CALL WALL STRIKE"])
        self.assertEqual(rows[0]["NET GEX VALUE"], rows[1]["NET GEX VALUE"])
        self.assertEqual(payload, self.store.db.execute("SELECT payload FROM samples WHERE id=1").fetchone()[0])

    def test_net_peak_change_does_not_change_legacy_retention_schedule(self):
        self.observe()
        self.assertFalse(self.observe(60, **{"POSITIVE NET GEX STRIKE": 115}))
        self.assertTrue(self.observe(900, **{"POSITIVE NET GEX STRIKE": 115}))
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.store.rows()[-1]["POSITIVE NET GEX STRIKE"], 115)

    def test_failed_csv_replace_preserves_database_and_old_export(self):
        self.observe()
        self.store.export()
        old = self.path.with_suffix(".csv").read_bytes()
        self.observe(900)
        with patch("utils.snapshot_history.os.replace", side_effect=PermissionError("test locked CSV")):
            with self.assertRaises(PermissionError):
                self.store.export()
        self.assertEqual(len(self.store.rows()), 2)
        self.assertEqual(self.path.with_suffix(".csv").read_bytes(), old)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])
        self.store.export()
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.path.with_suffix(".csv").read_text(encoding="utf-8-sig"))))), 2)


class WallStrengthHistoryTests(unittest.TestCase):
    def rows(self):
        changes = (
            {},
            {"CALL WALL STRENGTH": 10000, "PUT WALL STRENGTH": 15000},
            {"CALL WALL STRENGTH": 20000, "PUT WALL STRIKE": 95, "PUT WALL STRENGTH": 45000},
            {"CALL WALL STRIKE": 105, "CALL WALL STRENGTH": 50000, "PUT WALL STRENGTH": 12000},
        )
        return [
            dict(fixture_row(START + index * 900000, **change), SLNO=index + 1)
            for index, change in enumerate(changes)
        ]

    def test_per_side_strike_extrema_latest_valid_and_timestamps(self):
        rows = self.rows()
        summary = wall_strength_history(list(reversed(rows))).set_index(["Side", "Wall strike"])
        call = summary.loc[("Call", 110)]
        self.assertEqual((call["Low strength"], call["High strength"], call["Latest valid strength"]), (10000, 30000, 20000))
        self.assertEqual(call["Low captured (ET)"], rows[1]["TIMESTAMP"])
        self.assertEqual(call["High captured (ET)"], rows[0]["TIMESTAMP"])
        self.assertEqual(call["Latest valid capture (ET)"], rows[2]["TIMESTAMP"])
        self.assertEqual(call["Valid snapshots"], 3)
        put = summary.loc[("Put", 90)]
        self.assertEqual((put["Low strength"], put["High strength"], put["Latest valid strength"]), (12000, 30000, 12000))
        self.assertEqual(put["Valid snapshots"], 3)
        single = summary.loc[("Call", 105)]
        self.assertEqual((single["Low strength"], single["High strength"], single["Latest valid strength"]), (50000,) * 3)

    def test_missing_invalid_inputs_are_not_zero_or_latest_valid(self):
        rows = self.rows()
        rows.append(dict(
            fixture_row(START + 3600000, **{"CALL WALL STRENGTH": None, "PUT WALL STRENGTH": float("nan")}),
            SLNO=5,
        ))
        call = wall_strength_history(rows).set_index(["Side", "Wall strike"]).loc[("Call", 110)]
        self.assertEqual(call["Low strength"], 10000)
        self.assertEqual(call["Latest valid capture (ET)"], rows[2]["TIMESTAMP"])
        self.assertEqual(call["Valid snapshots"], 3)
        for bad in (None, -1, True, float("inf"), float("nan")):
            row = dict(fixture_row(**{"CALL WALL STRENGTH": bad, "PUT WALL STRIKE": None}), SLNO=1)
            self.assertTrue(wall_strength_history([row]).empty)
        self.assertTrue(wall_strength_history([]).empty)
        zero = dict(fixture_row(**{"CALL WALL STRENGTH": 0, "PUT WALL STRIKE": None}), SLNO=1)
        self.assertEqual(wall_strength_history([zero]).iloc[0]["Low strength"], 0)

    def test_latest_status_uses_snapshot_order_not_bar_size_or_filter(self):
        rows = self.rows()
        rows[-1]["CALL WALL STRENGTH"] = 1
        summary = wall_strength_history(list(reversed(rows))).set_index(["Side", "Wall strike"])
        self.assertEqual(summary.loc[("Call", 105), "Wall status"], "Latest wall")
        self.assertEqual(summary.loc[("Call", 110), "Wall status"], "Former wall")
        self.assertEqual(summary.loc[("Put", 90), "Wall status"], "Latest wall")
        self.assertEqual(summary.loc[("Put", 95), "Wall status"], "Former wall")
        self.assertTrue((summary["Latest snapshot (ET)"] == rows[-1]["TIMESTAMP"]).all())
        filtered = summary.loc[[("Call", 110)]]
        self.assertEqual(filtered.iloc[0]["Wall status"], "Former wall")
        rows[-1]["PUT WALL STRIKE"] = 105
        same_strike = wall_strength_history(rows)
        self.assertEqual(set(same_strike.loc[same_strike["Wall status"] == "Latest wall", "Side"]), {"Call", "Put"})

    def test_missing_latest_strike_or_strength_never_promotes_old_observations(self):
        rows = self.rows()
        rows.append(dict(rows[-1], SLNO=5, **{"CALL WALL STRENGTH": None, "PUT WALL STRIKE": None}))
        summary = wall_strength_history(rows).set_index(["Side", "Wall strike"])
        self.assertEqual(summary.loc[("Call", 105), "Wall status"], "Latest wall; strength unavailable")
        self.assertEqual(summary.loc[("Call", 110), "Wall status"], "Former wall")
        self.assertEqual(summary.loc[("Put", 90), "Wall status"], "Latest wall unavailable")
        rows[-1]["CALL WALL STRIKE"] = 120
        summary = wall_strength_history(rows)
        self.assertNotIn(120, summary["Wall strike"].values)
        self.assertTrue((summary.loc[summary["Side"] == "Call", "Wall status"] == "Former wall").all())

    def test_grouped_bars_have_signed_side_colors_and_watermark_error_ranges(self):
        from simple_dashboard import build_wall_strength_history_chart

        chart = build_wall_strength_history_chart(wall_strength_history(self.rows()))
        self.assertEqual(chart.layout.barmode, "group")
        call, put = chart.data
        self.assertEqual((call.marker.color, put.marker.color), ("#36cba5", "#f45b76"))
        self.assertEqual((call.error_y.color, put.error_y.color), ("#e5e7eb", "#e5e7eb"))
        self.assertEqual(list(call.x), ["105", "110"])
        self.assertEqual(list(call.y), [0.05, 0.02])
        self.assertEqual(list(call.text), ["LATEST", "FORMER"])
        self.assertEqual(list(call.marker.opacity), [1, 0.4])
        self.assertEqual(list(call.error_y.array), [0, 0.01])
        self.assertEqual(list(call.error_y.arrayminus), [0, 0.01])
        self.assertEqual(list(put.x), ["90", "95"])
        self.assertEqual(list(put.y), [0.012, 0.045])
        self.assertEqual(list(put.error_y.array), [0.018, 0])
        self.assertEqual(chart.layout.yaxis.title.text, "GEX ($ millions per 1% spot move)")

    def test_crossings_deduplicate_only_matching_scope_side_and_event(self):
        event = "UP 10/22/2026 10:01:00 EDT-0400 (sampled)"
        row = dict(fixture_row(), SLNO=1, **{
            "CALL WALL CROSSED": event, "PUT WALL CROSSED": event, "PUT WALL STRIKE": 110,
        })
        rows = [row, dict(row, SLNO=2), dict(row, SLNO=3, **{"SESSION DATE": "10/23/2026"}),
                dict(row, SLNO=4, **{"STRIKES ABOVE": 25}),
                dict(row, SLNO=5, **{"CALL WALL CROSSED": "Unrecognized annotation"})]
        before = csv_text(rows)
        events = crossing_history(rows)
        self.assertEqual(len(events), 7)
        self.assertEqual(set(events["Side"]), {"Call", "Put"})
        self.assertIn("Unrecognized annotation", events["Crossing (ET)"].values)
        self.assertEqual(csv_text(rows), before)
        self.assertTrue(crossing_history([]).empty)

    def test_capture_reason_excludes_crossings_without_changing_original_notes(self):
        remarks = "15-minute capture; Call wall changed 100 -> 110; unknown diagnostic; Call wall 110 crossed: UP now"
        self.assertEqual(capture_reason(remarks), "15-minute capture; Call wall changed 100 -> 110")
        self.assertEqual(capture_reason("unknown diagnostic"), "See full details")
        self.assertEqual(capture_reason(None), "See full details")


class CollectorTests(unittest.TestCase):
    def test_closed_or_disabled_collector_makes_no_broker_calls(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), tempfile.TemporaryDirectory() as directory:
                with HistoryStore(Path(directory) / "history.sqlite3") as store:
                    store.set("config", dict(DEFAULT_CONFIG, enabled=enabled))
                    stop = threading.Event()
                    with patch.object(stop, "wait", side_effect=lambda _: stop.set()), \
                         patch("history_recorder.market_state", return_value=MarketState(False, "Test closed", date(2026, 10, 23))), \
                         patch("history_recorder.collect_snapshot", side_effect=AssertionError("No broker calls")):
                        history_recorder.record_forever(store, stop)
                    self.assertEqual(store.get("status")["state"], "paused")
                    self.assertEqual(store.rows(), [])

    def test_open_collector_writes_sqlite_and_actual_csv_to_temp_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with HistoryStore(Path(directory) / "history.sqlite3") as store:
                stop = threading.Event()
                capture = make_gex_capture(
                    CONFIG, "261022", date(2026, 10, 22), dict(last=100, last_time=START),
                    DATA, list(DATA), START, "Background recorder",
                )
                with patch.object(stop, "wait", side_effect=lambda _: stop.set()), \
                     patch("history_recorder.market_state", return_value=MarketState(True, "Test open", date(2026, 10, 22))), \
                     patch("history_recorder.collect_snapshot", return_value=(fixture_row(), START, START, capture)):
                    history_recorder.record_forever(store, stop)
                self.assertEqual(len(store.rows()), 1)
                self.assertTrue(store.path.with_suffix(".csv").is_file())
                self.assertEqual(store.get("status")["state"], "recording")
                self.assertEqual(store.gex_rows(CONFIG, "261022", "2026-10-22"), [capture])

    def test_close_after_auth_prevents_socket_creation(self):
        opened = MarketState(True, "Test open", date(2026, 10, 22))
        closed = MarketState(False, "Test closed", date(2026, 10, 22))
        with patch("history_recorder.market_state", side_effect=[opened, opened, closed, closed]), \
             patch("history_recorder.ensure_streamer_token", return_value="test-only"), \
             patch("history_recorder.connect_websocket") as connect:
            with self.assertRaisesRegex(ValueError, "closed"):
                history_recorder.collect_snapshot(DEFAULT_CONFIG, opened)
            connect.assert_not_called()

    def test_full_object_handshake_and_failure_cleanup(self):
        ws = Mock()
        ws.recv.side_effect = [json.dumps(message) for message in (
            dict(type="SETUP"), dict(type="AUTH_STATE", state="UNAUTHORIZED"),
            dict(type="AUTH_STATE", state="AUTHORIZED"), dict(type="CHANNEL_OPENED", channel=1),
            dict(type="FEED_CONFIG", channel=1, dataFormat="FULL"),
        )]
        with patch("utils.snapshot_feed.create_connection", return_value=ws), \
             patch("utils.snapshot_feed.get_dxlink_url", return_value="wss://test.invalid"):
            self.assertIs(connect_websocket("test-only"), ws)
        setup = [json.loads(call.args[0]) for call in ws.send.call_args_list]
        self.assertTrue(any(item.get("acceptDataFormat") == "FULL" for item in setup))
        ws.close.assert_not_called()
        ws.recv.side_effect = [json.dumps(dict(type="ERROR"))]
        with patch("utils.snapshot_feed.create_connection", return_value=ws), \
             patch("utils.snapshot_feed.get_dxlink_url", return_value="wss://test.invalid"):
            with self.assertRaises(ValueError):
                connect_websocket("test-only")
        ws.close.assert_called_once()

    def test_real_http_health_and_exclusive_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with HistoryStore(path) as store:
                store.set("status", dict(state="paused", heartbeat_ms=history_recorder.now_ms()))
            server = history_recorder.ExclusiveHealthServer(("127.0.0.1", 0), history_recorder.HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("utils.snapshot_history.HISTORY_DB", path):
                    with urlopen(f"http://127.0.0.1:{server.server_port}/health", timeout=5) as response:
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.load(response)["persisted_rows"], 0)
                with self.assertRaises(OSError):
                    history_recorder.ExclusiveHealthServer(("127.0.0.1", server.server_port), history_recorder.HealthHandler)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class HistoryDashboardTests(unittest.TestCase):
    def test_history_download_and_metrics_visible_without_live_broker_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with HistoryStore(path) as store:
                store.observe(fixture_row(), START, START)
                store.observe(fixture_row(START + 60000, 111), START + 60000, START + 60000)
                next_day = START + 86400000
                store.observe(
                    fixture_row(next_day, session=date(2026, 10, 23), **{
                        "SYMBOL": "QQQ", "OPTION ROOT": "QQQ", "EXPIRY": "10/23/2026",
                        "CALL WALL STRIKE": 999, "CALL WALL STRENGTH": 1e12,
                    }), next_day, next_day,
                )
            app = AppTest.from_string("""
import streamlit as st
from simple_dashboard import render_snapshot_history
render_snapshot_history("SPY", "SPY", "261022", 50, 50, 1, True)
""")
            with patch("utils.snapshot_history.HISTORY_DB", path), \
                 patch("streamlit.download_button", wraps=st.download_button) as download:
                app.run(timeout=30)
            self.assertFalse(app.exception)
            table = app.dataframe[0].value
            self.assertEqual(table.iloc[0]["CURRENT PRICE"], 100)
            self.assertEqual(list(table.columns), [*COLUMNS[:11], "CAPTURE REASON"])
            self.assertEqual(table.iloc[0]["CAPTURE REASON"], "First collected snapshot")
            details = app.dataframe[1].value
            self.assertEqual(list(details.columns), COLUMNS)
            self.assertIn("UP", details.iloc[0]["CALL WALL CROSSED"])
            self.assertEqual(details.iloc[0]["CALL WALL OI"], 30)
            self.assertIn("Call wall 110 crossed", details.iloc[0]["REMARKS"])
            self.assertIn("Full snapshot details (all original columns)", [item.label for item in app.expander])
            self.assertIn("Sampled crossings (unique events)", [item.label for item in app.expander])
            self.assertTrue(all(not item.proto.expanded for item in app.expander))
            self.assertEqual(len(table), 1)
            self.assertEqual(len(app.get("download_button")), 1)
            self.assertEqual(len(app.get("plotly_chart")), 1)
            watermarks = next(item.value for item in app.dataframe if "Wall status" in item.value)
            self.assertNotIn(999, watermarks["Wall strike"].values)
            self.assertEqual(set(watermarks["Side"]), {"Call", "Put"})
            self.assertEqual(download.call_args.kwargs["file_name"], "snapshot_history.csv")
            exported = list(csv.DictReader(io.StringIO(download.call_args.kwargs["data"].decode("utf-8-sig"))))
            self.assertEqual([row["SYMBOL"] for row in exported], ["SPY", "QQQ"])
            self.assertEqual([row["SESSION DATE"] for row in exported], ["10/22/2026", "10/23/2026"])

    def test_watermarks_follow_strike_selection_and_selected_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with HistoryStore(path) as store:
                store.observe(fixture_row(), START, START)
                store.observe(
                    fixture_row(START + 900000, **{"CALL WALL STRENGTH": 20000}),
                    START + 900000, START + 900000,
                )
                next_day = START + 86400000
                store.observe(
                    fixture_row(next_day, session=date(2026, 10, 23), **{"CALL WALL STRENGTH": 99000}),
                    next_day, next_day,
                )
            app = AppTest.from_string("""
from simple_dashboard import render_snapshot_history
render_snapshot_history("SPY", "SPY", "261022", 50, 50, 1, True)
""")
            with patch("utils.snapshot_history.HISTORY_DB", path):
                app.run(timeout=30)
                self.assertFalse(app.exception)
                self.assertEqual(next(item.value for item in app.dataframe if "Wall status" in item.value).iloc[0]["High strength"], 99000)
                app.selectbox(key="history_session").set_value("10/22/2026").run(timeout=30)
                self.assertFalse(app.exception)
                app.multiselect(key="wall_strength_strikes").set_value([110.0]).run(timeout=30)
                self.assertFalse(app.exception)
                watermarks = next(item.value for item in app.dataframe if "Wall status" in item.value)
                self.assertEqual(len(watermarks), 1)
                self.assertEqual(watermarks.iloc[0]["Side"], "Call")
                self.assertEqual(watermarks.iloc[0]["Low strength"], 20000)
                self.assertEqual(watermarks.iloc[0]["High strength"], 30000)
                self.assertEqual(watermarks.iloc[0]["Latest valid strength"], 20000)
                self.assertEqual(watermarks.iloc[0]["Wall status"], "Latest wall")
                self.assertTrue(any("sampled watermarks" in item.value for item in app.caption))

    def test_hidden_latest_strike_does_not_relabel_former_bar(self):
        app = AppTest.from_string("""
from simple_dashboard import render_wall_strength_history
render_wall_strength_history([
    {"SLNO": 1, "TIMESTAMP": "10/22/2026 10:00:00 EDT-0400", "CALL WALL STRIKE": 100, "CALL WALL STRENGTH": 500},
    {"SLNO": 2, "TIMESTAMP": "10/22/2026 10:15:00 EDT-0400", "CALL WALL STRIKE": 110, "CALL WALL STRENGTH": None},
])
""").run(timeout=30)
        self.assertFalse(app.exception)
        app.multiselect(key="wall_strength_strikes").set_value([100.0]).run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.dataframe[0].value.iloc[0]["Wall status"], "Former wall")
        self.assertEqual(app.metric[0].value, "110.00")
        self.assertEqual(app.metric[1].value, "Unavailable")
        self.assertTrue(any("Latest snapshot strength unavailable" in item.value for item in app.caption))


if __name__ == "__main__":
    unittest.main()
