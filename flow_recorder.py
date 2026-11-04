"""Standalone daily-expiry recorder. Start before the session; no historical-flow backfill."""
import json
import logging
import os
import signal
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from websocket import create_connection, WebSocketException, WebSocketTimeoutException

from utils.auth import get_dxlink_url, get_streamer_token
from utils.flow_store import FLOW_DB, NEW_YORK, FlowStore, read_recorder_status
from utils.option_chain import fetch_expiry_chain
from utils.market_hours import market_state


UNDERLYING = "SPX"
OPTION_ROOT = "SPXW"
HEALTH_PORT = 8502


class RecorderError(RuntimeError):
    pass


def now_ms():
    return int(time.time() * 1000)


def send(ws, message_type, channel=0, **fields):
    ws.send(json.dumps({"type": message_type, "channel": channel, **fields}))


def check_message(message):
    if message.get("type") in ("ERROR", "CHANNEL_CLOSED"):
        raise RecorderError("Provider reported a feed/channel error.")
    if message.get("type") == "AUTH_STATE" and message.get("state") != "AUTHORIZED":
        raise RecorderError("Provider authorization expired or was rejected.")


def connect_feed(token):
    ws = create_connection(get_dxlink_url(), timeout=10)
    ready = False
    try:
        send(ws, "SETUP", version="1.0.0", keepaliveTimeout=60, acceptKeepaliveTimeout=60)
        deadline = time.monotonic() + 25
        authorized = auth_sent = False
        configured = set()
        while time.monotonic() < deadline:
            ws.settimeout(1)
            try:
                message = json.loads(ws.recv())
            except WebSocketTimeoutException:
                continue
            if message.get("type") == "AUTH_STATE" and not authorized:
                if message.get("state") == "UNAUTHORIZED" and not auth_sent:
                    send(ws, "AUTH", token=token)
                    auth_sent = True
                elif message.get("state") == "AUTHORIZED":
                    authorized = True
                    for channel, contract in ((1, "STREAM"), (3, "AUTO")):
                        send(ws, "CHANNEL_REQUEST", channel, service="FEED", parameters={"contract": contract})
                else:
                    raise RecorderError("Provider rejected authorization.")
            elif message.get("type") == "CHANNEL_OPENED":
                send(ws, "FEED_SETUP", message["channel"], acceptDataFormat="FULL", acceptAggregationPeriod=0.1)
            elif message.get("type") == "FEED_CONFIG":
                if message.get("dataFormat") != "FULL":
                    raise RecorderError("Provider did not configure full-object event format.")
                configured.add(message["channel"])
                if authorized and {1, 3}.issubset(configured):
                    ready = True
                    return ws
            else:
                check_message(message)
        raise RecorderError("Feed handshake exceeded 25 seconds.")
    finally:
        if not ready:
            ws.close()


def subscribe_options(ws, contracts):
    # STREAM has no fromTime: historical capacity limits cannot masquerade as a full tape.
    send(ws, "FEED_SUBSCRIPTION", 1, add=[
        {"symbol": symbol, "type": "TimeAndSale"} for symbol in sorted(contracts)
    ])


def describe_error(error):
    if isinstance(error, RecorderError):
        return str(error)
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"Option-chain HTTP {error.response.status_code}"
    if isinstance(error, ValueError):
        return "Invalid chain, event, or feed payload; recording interval ended."
    return f"Connection failure ({type(error).__name__})"


def record_forever(store, stop):
    force_refresh = False
    while not stop.is_set():
        state = dict(
            state="connecting", pid=os.getpid(), underlying=UNDERLYING, root=OPTION_ROOT,
            expiry=None, heartbeat_ms=now_ms(), last_feed_ms=None, last_option_ms=None,
            contract_count=0, error=None,
        )
        ws = connection_id = None
        reason = "Stopped"
        try:
            session = market_state(UNDERLYING, OPTION_ROOT)
            expiry = session.session_date.strftime("%y%m%d") if session.session_date else None
            state["expiry"] = expiry
            if not session.allowed:
                state.update(
                    state="paused", error=None, pause_reason=session.label,
                    next_open=session.next_open.isoformat() if session.next_open else None,
                )
                store.set_status(**state)
                stop.wait(3)
                continue
            store.set_status(**state)
            contracts = fetch_expiry_chain(UNDERLYING, OPTION_ROOT, expiry)
            if not market_state(UNDERLYING, OPTION_ROOT, expiry).allowed:
                continue
            token = get_streamer_token(force_refresh=force_refresh)
            force_refresh = False
            if not market_state(UNDERLYING, OPTION_ROOT, expiry).allowed:
                continue
            ws = connect_feed(token)
            if not market_state(UNDERLYING, OPTION_ROOT, expiry).allowed:
                continue
            started = now_ms()
            connection_id = store.begin(UNDERLYING, expiry, contracts, started)
            subscribe_options(ws, contracts)
            start = datetime.now(NEW_YORK).replace(hour=9, minute=30, second=0, microsecond=0)
            send(ws, "FEED_SUBSCRIPTION", 3, add=[
                {"symbol": UNDERLYING + "{=1m}", "type": "Candle", "fromTime": int(start.timestamp() * 1000)}
            ])
            state.update(state="listening", contract_count=len(contracts))
            store.set_status(**state)
            logging.info("Live subscription requested: %s %s, %d contracts", OPTION_ROOT, expiry, len(contracts))
            last_keepalive = last_heartbeat = last_chain_check = last_token_check = time.monotonic()
            while not stop.is_set():
                moment = time.monotonic()
                session = market_state(UNDERLYING, OPTION_ROOT)
                if not session.allowed or session.session_date.strftime("%y%m%d") != expiry:
                    reason = "Scheduled session closed or active PM expiry changed"
                    break
                if moment - last_keepalive >= 15:
                    send(ws, "KEEPALIVE")
                    last_keepalive = moment
                if moment - last_heartbeat >= 3:
                    state["heartbeat_ms"] = now_ms()
                    store.heartbeat(connection_id, state["heartbeat_ms"], len(contracts))
                    store.set_status(**state)
                    last_heartbeat = moment
                if moment - last_chain_check >= 900:
                    refreshed = fetch_expiry_chain(UNDERLYING, OPTION_ROOT, expiry)
                    added = {symbol: value for symbol, value in refreshed.items() if symbol not in contracts}
                    if added:
                        with store.db:
                            store.add_contracts(UNDERLYING, expiry, added, now_ms())
                        subscribe_options(ws, added)
                        contracts.update(added)
                        state["contract_count"] = len(contracts)
                    last_chain_check = time.monotonic()
                if moment - last_token_check >= 900:
                    refreshed_token = get_streamer_token()
                    if refreshed_token != token:
                        reason = "Quote-token renewal"
                        break
                    last_token_check = time.monotonic()
                ws.settimeout(1)
                try:
                    raw = ws.recv()
                except WebSocketTimeoutException:
                    continue
                if not raw:
                    raise RecorderError("Provider closed the connection.")
                message = json.loads(raw)
                check_message(message)
                if message.get("type") == "FEED_DATA":
                    events = message.get("data")
                    if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                        raise RecorderError("Unexpected feed event format.")
                    received = now_ms()
                    store.apply_events(connection_id, events, received)
                    state["last_feed_ms"] = received
                    if any(event.get("eventType") == "TimeAndSale" for event in events):
                        state["last_option_ms"] = received
        except (RecorderError, requests.RequestException, WebSocketException, OSError, ValueError) as error:
            reason = describe_error(error)
            force_refresh = isinstance(error, RecorderError) and "authorization" in reason.lower()
            state.update(state="retrying", error=reason, heartbeat_ms=now_ms())
            store.set_status(**state)
            logging.error("%s Retrying in 30 seconds.", reason)
        finally:
            if ws is not None:
                ws.close()
            if connection_id is not None:
                store.end(connection_id, now_ms(), reason)
        if state["state"] == "retrying":
            stop.wait(30)


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        state = read_recorder_status()
        fresh = now_ms() - state.get("heartbeat_ms", 0) < 15000
        payload = json.dumps({
            **state, "heartbeat_fresh": fresh,
            "database_bytes": FLOW_DB.stat().st_size if FLOW_DB.exists() else 0,
        }).encode()
        self.send_response(200 if fresh and state.get("state") in {"listening", "paused"} else 503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


class ExclusiveHealthServer(ThreadingHTTPServer):
    allow_reuse_address = False


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Bind before recovering open intervals: a second launcher must not modify the active writer's DB.
    server = ExclusiveHealthServer(("127.0.0.1", HEALTH_PORT), HealthHandler)
    stop = threading.Event()
    store = FlowStore()
    store.recover()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        record_forever(store, stop)
    finally:
        store.set_status(
            state="stopped" if stop.is_set() else "failed", pid=os.getpid(), heartbeat_ms=now_ms(),
            underlying=UNDERLYING, root=OPTION_ROOT,
            error=None if stop.is_set() else "Collector exited unexpectedly; inspect its console.",
        )
        server.shutdown()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
