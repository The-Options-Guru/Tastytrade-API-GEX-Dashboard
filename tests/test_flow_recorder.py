import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import requests

import flow_recorder
from simple_dashboard import build_flow_chart
from utils.flow_store import FlowStore, NEW_YORK, read_flow_snapshot, read_recorder_status
from utils.market_hours import MarketState


CALL, PUT = ".SPXW261022C7665", ".SPXW261022P7665"
CONTRACTS = {
    CALL: {"side": "call", "multiplier": 100, "strike": 7665},
    PUT: {"side": "put", "multiplier": 100, "strike": 7665},
}
START = int(datetime(2026, 10, 22, 9, 30, tzinfo=NEW_YORK).timestamp() * 1000)


def trade(index=1, **changes):
    event = dict(
        eventType="TimeAndSale", eventSymbol=CALL, index=index, time=START + 1000,
        type="NEW", eventFlags=0, validTick=True, spreadLeg=False,
        price=2, size=3, aggressorSide="BUY", tradeId=123,
    )
    return {**event, **changes}


class FlowStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "flow.sqlite3"
        self.store = FlowStore(self.path)
        self.connection = self.store.begin("SPX", "261022", CONTRACTS, START)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def apply(self, *events, connection=None):
        self.store.apply_events(connection or self.connection, list(events), START + 10000)

    def snapshot(self):
        return read_flow_snapshot(self.path, "SPX", "261022")

    def test_known_signed_premium_control_and_duplicate_replay(self):
        events = [
            trade(1), trade(2, aggressorSide="SELL"),
            trade(1, eventSymbol=PUT), trade(2, eventSymbol=PUT, aggressorSide="SELL", size=4),
        ]
        self.apply(*events, *events)
        snapshot = self.snapshot()
        self.assertEqual(sorted(row["signed_premium"] for row in snapshot["trades"]), [-600, -600, 600, 800])
        self.assertEqual(sum(row["signed_premium"] for row in snapshot["minutes"]), 200)
        self.assertEqual(snapshot["counts"], [{"reason": None, "count": 4}])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM updates").fetchone()[0], 4)

    def test_correction_replaces_and_cancellation_retracts_even_invalid_tick(self):
        self.apply(trade())
        self.apply(trade(type="CORRECTION", price=4))
        self.assertEqual(self.snapshot()["trades"][0]["signed_premium"], 1200)
        self.apply(trade(type="CANCEL", validTick=False, price=None, size=None))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["trades"], [])
        self.assertEqual(snapshot["minutes"], [])
        self.assertEqual(snapshot["counts"], [{"reason": "cancelled", "count": 1}])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM updates").fetchone()[0], 3)

    def test_unmatched_corrections_never_become_new_premium(self):
        self.apply(trade(type="CORRECTION"), trade(type="CORRECTION", price=8))
        self.assertEqual(self.snapshot()["minutes"], [])
        self.assertEqual(self.snapshot()["counts"][0]["reason"], "unmatched correction")

    def test_unknown_spread_invalid_and_old_history_excluded_not_classified_zero(self):
        self.apply(
            trade(1, aggressorSide="UNDEFINED"), trade(2, spreadLeg=True),
            trade(3, validTick=False), trade(4, price="NaN"),
            trade(5, time=START - 1000), trade(6, size=True),
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot["trades"], [])
        self.assertEqual(snapshot["minutes"], [])
        reasons = {row["reason"]: row["count"] for row in snapshot["counts"]}
        self.assertEqual(reasons["unclassified aggressor"], 1)
        self.assertEqual(reasons["invalid price or size"], 2)
        self.apply(trade(5, type="CORRECTION", time=START - 1000, price=8))
        self.assertEqual(self.snapshot()["minutes"], [])

    def test_new_contract_starts_at_subscription_not_initial_connection(self):
        later = ".SPXW261022C7670"
        with self.store.db:
            self.store.add_contracts("SPX", "261022", {later: CONTRACTS[CALL]}, START + 5000)
        self.apply(trade(eventSymbol=later, time=START + 1000))
        self.assertEqual(self.snapshot()["minutes"], [])
        self.assertEqual(self.snapshot()["counts"][0]["reason"], "before recording window")

    def test_reconnect_correction_and_restart_preserve_records_and_gap(self):
        self.apply(trade())
        self.store.end(self.connection, START + 2000, "Network disconnect")
        second = self.store.begin("SPX", "261022", CONTRACTS, START + 5000)
        self.apply(trade(type="CORRECTION", price=5), connection=second)
        self.apply(trade(2, time=START + 6000), connection=second)
        self.store.heartbeat(second, START + 9000, 2)
        self.store.close()
        self.store = FlowStore(self.path)
        self.store.recover()
        snapshot = self.snapshot()
        self.assertEqual(sum(row["signed_premium"] for row in snapshot["minutes"]), 2100)
        self.assertEqual([row["ended_ms"] for row in snapshot["intervals"]], [START + 2000, START + 9000])
        self.assertEqual(snapshot["intervals"][1]["reason"], "Recorder restarted")
        chart = build_flow_chart(snapshot, "SPX", "261022")
        cumulative = next(trace for trace in chart.data if trace.name == "Cumulative recorded net premium")
        self.assertEqual(list(cumulative.y), [0.0015, None, 0.0021])
        self.assertEqual(len(chart.layout.shapes), 1)

    def test_live_flags_bad_identity_and_wrong_universe_fail_closed_atomically(self):
        for event in (
            trade(2, eventFlags=1), trade(2, eventFlags=2), trade(2, eventFlags=4),
            trade(index=None), trade(2, eventSymbol=".SPXW261023C7665"),
            trade(2, type="UNKNOWN"),
        ):
            with self.subTest(event=event):
                with self.assertRaises(ValueError):
                    self.apply(trade(1), event)
                self.assertEqual(self.snapshot()["minutes"], [])

    def test_candle_history_is_separate_and_never_creates_option_flow(self):
        self.apply(
            dict(eventType="Candle", eventSymbol="SPX{=m}", time=START, close=7660, eventFlags=4),
            dict(eventType="Candle", eventSymbol="SPX{=m}", time=START, close=7661, eventFlags=0),
            dict(eventType="Candle", eventSymbol="SPX{=m}", time=START - 60000, close="NaN", eventFlags=10),
        )
        snapshot = self.snapshot()
        self.assertEqual(snapshot["candles"], [{"time_ms": START, "close": 7661}])
        self.assertFalse(snapshot["minutes"])
        chart = build_flow_chart(snapshot, "SPX", "261022")
        self.assertEqual(len(chart.data), 1)
        self.assertIn("unavailable", chart.layout.annotations[0].text)
        self.assertEqual(read_flow_snapshot(self.path, "SPY", "261022")["candles"], [])
        self.assertEqual(read_flow_snapshot(self.path, "SPX", "261023")["trades"], [])

    def test_real_bubbles_use_containing_candle_and_bounded_largest_prints(self):
        self.apply(dict(eventType="Candle", eventSymbol="SPX{=m}", time=START, close=7661))
        self.apply(*[trade(i, size=i + 1) for i in range(205)])
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["trades"]), 200)
        self.assertEqual(snapshot["counts"][0]["count"], 205)
        self.assertEqual(sum(row["signed_premium"] for row in snapshot["minutes"]), 200 * sum(range(1, 206)))
        chart = build_flow_chart(snapshot, "SPX", "261022")
        bubbles = next(trace for trace in chart.data if trace.mode == "markers")
        self.assertEqual(len(bubbles.x), 200)
        self.assertEqual(set(bubbles.y), {7661})
        self.assertEqual(bubbles.x[0].hour, 9)

    def test_readers_do_not_create_missing_database_and_status_is_durable(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        self.assertFalse(read_flow_snapshot(missing, "SPX", "261022")["status"])
        self.assertFalse(missing.exists())
        self.store.set_status(state="listening", heartbeat_ms=START)
        self.assertEqual(read_recorder_status(self.path), {"state": "listening", "heartbeat_ms": START})


class FakeSocket:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        return json.dumps(next(self.messages))

    def settimeout(self, timeout):
        pass

    def close(self):
        self.closed = True


class RecorderProtocolTests(unittest.TestCase):
    def test_handshake_live_only_trade_contract_and_history_price_channel(self):
        ws = FakeSocket([
            {"type": "SETUP"},
            {"type": "AUTH_STATE", "state": "UNAUTHORIZED"},
            {"type": "AUTH_STATE", "state": "AUTHORIZED"},
            {"type": "CHANNEL_OPENED", "channel": 1},
            {"type": "CHANNEL_OPENED", "channel": 3},
            {"type": "FEED_CONFIG", "channel": 1, "dataFormat": "FULL"},
            {"type": "FEED_CONFIG", "channel": 3, "dataFormat": "FULL"},
        ])
        with patch("flow_recorder.create_connection", return_value=ws), patch("flow_recorder.get_dxlink_url", return_value="wss://example.test"):
            self.assertIs(flow_recorder.connect_feed("test-token"), ws)
        requests_sent = [message for message in ws.sent if message["type"] == "CHANNEL_REQUEST"]
        self.assertEqual([message["parameters"]["contract"] for message in requests_sent], ["STREAM", "AUTO"])
        flow_recorder.subscribe_options(ws, CONTRACTS)
        self.assertTrue(all("fromTime" not in item for item in ws.sent[-1]["add"]))
        self.assertTrue(all(item["type"] == "TimeAndSale" for item in ws.sent[-1]["add"]))

    def test_failed_auth_closes_socket(self):
        ws = FakeSocket([{"type": "AUTH_STATE", "state": "UNAUTHORIZED"}] * 2)
        with patch("flow_recorder.create_connection", return_value=ws), patch("flow_recorder.get_dxlink_url", return_value="wss://example.test"):
            with self.assertRaises(flow_recorder.RecorderError):
                flow_recorder.connect_feed("test-token")
        self.assertTrue(ws.closed)

    def test_collector_writes_received_frame_then_closes_interval(self):
        stop = threading.Event()
        ws = FakeSocket([])
        def receive():
            stop.set()
            return json.dumps({"type": "FEED_DATA", "channel": 1, "data": [trade()]})
        ws.recv = receive
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.sqlite3"
            store = FlowStore(path)
            try:
                with patch("flow_recorder.fetch_expiry_chain", return_value=CONTRACTS), \
                     patch("flow_recorder.get_streamer_token", return_value="test-token"), \
                     patch("flow_recorder.connect_feed", return_value=ws), \
                     patch("flow_recorder.market_state", return_value=MarketState(True, "Test session", date(2026, 10, 22))), \
                     patch("flow_recorder.now_ms", return_value=START), \
                     patch("flow_recorder.datetime") as clock:
                    clock.now.return_value = datetime(2026, 10, 22, 9, 30, tzinfo=NEW_YORK)
                    flow_recorder.record_forever(store, stop)
                snapshot = read_flow_snapshot(path, "SPX", "261022")
                self.assertEqual(snapshot["trades"][0]["signed_premium"], 600)
                self.assertEqual(snapshot["intervals"][0]["ended_ms"], START)
                self.assertTrue(ws.closed)
            finally:
                store.close()

    def test_closed_collector_heartbeats_without_any_broker_access(self):
        stop = threading.Event()
        def wait(seconds):
            stop.set()
        stop.wait = wait
        with tempfile.TemporaryDirectory() as folder:
            store = FlowStore(Path(folder) / "flow.sqlite3")
            try:
                with patch("flow_recorder.market_state", return_value=MarketState(False, "Test closed", date(2026, 10, 23))), \
                     patch("flow_recorder.fetch_expiry_chain", side_effect=AssertionError("No closed-market chain call")), \
                     patch("flow_recorder.get_streamer_token", side_effect=AssertionError("No closed-market auth")), \
                     patch("flow_recorder.connect_feed", side_effect=AssertionError("No closed-market socket")):
                    flow_recorder.record_forever(store, stop)
                status = read_recorder_status(store.path)
                self.assertEqual(status["state"], "paused")
                self.assertEqual(status["expiry"], "261023")
                self.assertIsInstance(status["heartbeat_ms"], int)
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM connections").fetchone()[0], 0)
            finally:
                store.close()

    def test_scheduled_close_disconnects_before_reading_or_refreshing(self):
        stop = threading.Event()
        ws = FakeSocket([])
        def wait(seconds):
            stop.set()
        stop.wait = wait
        opened = MarketState(True, "Test open", date(2026, 10, 23))
        closed = MarketState(False, "Test closed", date(2026, 10, 23))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.sqlite3"
            store = FlowStore(path)
            try:
                with patch("flow_recorder.market_state", side_effect=[opened] * 4 + [closed, closed]), \
                     patch("flow_recorder.fetch_expiry_chain", return_value=CONTRACTS) as chain, \
                     patch("flow_recorder.get_streamer_token", return_value="test-token"), \
                     patch("flow_recorder.connect_feed", return_value=ws):
                    flow_recorder.record_forever(store, stop)
                chain.assert_called_once_with("SPX", "SPXW", "261023")
                self.assertTrue(ws.closed)
                interval = read_flow_snapshot(path, "SPX", "261023")["intervals"][0]
                self.assertIn("Scheduled session closed", interval["reason"])
                self.assertEqual(read_recorder_status(path)["state"], "paused")
            finally:
                store.close()

    def test_errors_do_not_echo_provider_response_or_credentials(self):
        self.assertEqual(
            flow_recorder.describe_error(requests.RequestException("do-not-log-body")),
            "Connection failure (RequestException)",
        )
        self.assertNotIn("do-not-log-body", flow_recorder.describe_error(ValueError("do-not-log-body")))

    def test_healthy_paused_http_response_and_exclusive_listener(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "flow.sqlite3"
            store = FlowStore(path)
            store.set_status(state="paused", heartbeat_ms=flow_recorder.now_ms(), pause_reason="Scheduled closure")
            server = flow_recorder.ExclusiveHealthServer(("127.0.0.1", 0), flow_recorder.HealthHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch("flow_recorder.FLOW_DB", path), \
                     patch("flow_recorder.read_recorder_status", side_effect=lambda: read_recorder_status(path)):
                    response = requests.get(f"http://127.0.0.1:{server.server_port}/health", timeout=3)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], "paused")
                self.assertTrue(response.json()["heartbeat_fresh"])
                with self.assertRaises(OSError):
                    flow_recorder.ExclusiveHealthServer(("127.0.0.1", server.server_port), flow_recorder.HealthHandler)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)
                store.close()


if __name__ == "__main__":
    unittest.main()
