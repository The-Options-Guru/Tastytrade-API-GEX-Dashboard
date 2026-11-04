"""Unattended scheduled snapshots: poll each minute, retain 15-minute/wall-change history."""
import json
import logging
import os
import signal
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from websocket import WebSocketException

from utils.auth import ensure_streamer_token
from utils.market_hours import market_state
from utils.snapshot_feed import connect_websocket, fetch_option_data, generate_option_symbols, get_underlying_snapshot
from utils.snapshot_history import DEFAULT_CONFIG, HistoryStore, make_row, validate_config
from utils.intraday_gex import make_gex_capture


HEALTH_PORT = 8504


def now_ms():
    return int(time.time() * 1000)


def collect_snapshot(config, session):
    expiry = config["expiry"] or session.session_date.strftime("%y%m%d")

    def guard():
        current = market_state(config["symbol"], config["root"], expiry)
        active = market_state(config["symbol"], config["root"])
        if not current.allowed or (config["expiry"] is None and active.session_date != session.session_date):
            raise ValueError("Scheduled history session closed or active expiry changed.")

    guard()
    token = ensure_streamer_token()
    guard()
    ws = connect_websocket(token)
    try:
        guard()
        prices = get_underlying_snapshot(ws, config["symbol"], session_guard=guard)
        spot = prices.get("last")
        if not spot:
            raise ValueError("No underlying trade observed; history does not record fallback prices.")
        symbols = generate_option_symbols(
            spot, config["root"], expiry, config["strikes_up"], config["strikes_down"], config["increment"],
        )
        options = fetch_option_data(ws, symbols, wait_seconds=20, session_guard=guard)
        guard()
    finally:
        ws.close()
    at_ms = now_ms()
    row = make_row(config, expiry, session.session_date, prices, options, symbols, at_ms)
    capture = make_gex_capture(config, expiry, session.session_date, prices, options, symbols, at_ms, "Background recorder")
    return row, at_ms, prices.get("last_time"), capture


def record_forever(store, stop):
    while not stop.is_set():
        started = time.monotonic()
        state = dict(state="collecting", pid=os.getpid(), heartbeat_ms=now_ms(), error=None)
        try:
            config = validate_config(store.get("config", DEFAULT_CONFIG))
            session = market_state(config["symbol"], config["root"], config["expiry"])
            state.update(
                symbol=config["symbol"], root=config["root"],
                expiry=config["expiry"] or (session.session_date.strftime("%y%m%d") if session.session_date else None),
                strikes_up=config["strikes_up"], strikes_down=config["strikes_down"],
            )
            if not config["enabled"] or not session.allowed:
                state.update(
                    state="paused", pause_reason=session.label if config["enabled"] else "Background history disabled",
                    next_open=session.next_open.isoformat() if session.next_open and config["enabled"] else None,
                )
                store.set("status", state)
                stop.wait(3)
                continue
            store.set("status", state)
            row, at_ms, price_ms, capture = collect_snapshot(config, session)
            if stop.is_set():
                break
            changed = store.observe(row, at_ms, price_ms)
            store.save_gex_capture(capture)
            # Rebuild after every observation: also recovers an interrupted CSV replacement.
            store.export()
            state.update(
                state="recording", heartbeat_ms=now_ms(), last_observation_ms=at_ms,
                price_timestamp=row["PRICE TIMESTAMP"], oi_coverage=row["OI COVERAGE"],
                gamma_coverage=row["GAMMA COVERAGE"], volume_coverage=row["VOLUME COVERAGE"],
                csv_updated=changed,
            )
            store.set("status", state)
            logging.info("Snapshot persisted/compared: %s %s; OI %s, gamma %s; retained change=%s",
                         config["symbol"], state["expiry"], row["OI COVERAGE"], row["GAMMA COVERAGE"], changed)
            stop.wait(max(0, 60 - (time.monotonic() - started)))
        except (requests.RequestException, WebSocketException, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
            state.update(state="retrying", heartbeat_ms=now_ms(), error=f"History capture failed ({type(error).__name__})")
            store.set("status", state)
            logging.error("%s; no replacement snapshot fabricated. Retrying in 30 seconds.", state["error"])
            stop.wait(30)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        with HistoryStore() as store:
            status = store.get("status", {})
            count = store.db.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        fresh = 0 <= now_ms() - status.get("heartbeat_ms", 0) < 90000
        payload = json.dumps({**status, "heartbeat_fresh": fresh, "persisted_rows": count}).encode()
        self.send_response(200 if fresh and status.get("state") in {"paused", "collecting", "recording"} else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        return


class ExclusiveHealthServer(ThreadingHTTPServer):
    allow_reuse_address = False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ExclusiveHealthServer(("127.0.0.1", HEALTH_PORT), HealthHandler)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with HistoryStore() as store:
            if store.get("config") is None:
                store.set("config", DEFAULT_CONFIG)
            try:
                record_forever(store, stop)
            finally:
                store.set("status", dict(
                    state="stopped" if stop.is_set() else "failed", pid=os.getpid(), heartbeat_ms=now_ms(),
                    error=None if stop.is_set() else "History collector exited; inspect its console.",
                ))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
