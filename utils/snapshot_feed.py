"""Shared bounded snapshot reads for the dashboard and unattended history collector."""
import json
import time

from websocket import create_connection, WebSocketTimeoutException

from utils.auth import get_dxlink_url
from utils.flow_store import finite_number


def connect_websocket(token):
    ws = create_connection(get_dxlink_url(), timeout=10)
    ready = False
    try:
        ws.send(json.dumps({
            "type": "SETUP", "channel": 0, "keepaliveTimeout": 60,
            "acceptKeepaliveTimeout": 60, "version": "1.0.0",
        }))
        deadline = time.monotonic() + 25
        auth_sent = False
        while time.monotonic() < deadline:
            ws.settimeout(1)
            try:
                message = json.loads(ws.recv())
            except WebSocketTimeoutException:
                continue
            kind = message.get("type")
            if kind in {"ERROR", "CHANNEL_CLOSED"}:
                raise ValueError("Provider rejected the snapshot feed/channel.")
            if kind == "AUTH_STATE":
                state = message.get("state")
                if state == "UNAUTHORIZED" and not auth_sent:
                    ws.send(json.dumps({"type": "AUTH", "channel": 0, "token": token}))
                    auth_sent = True
                elif state == "AUTHORIZED":
                    ws.send(json.dumps({
                        "type": "CHANNEL_REQUEST", "channel": 1, "service": "FEED",
                        "parameters": {"contract": "AUTO"},
                    }))
                else:
                    raise ValueError("Provider rejected snapshot authorization.")
            elif kind == "CHANNEL_OPENED" and message.get("channel") == 1:
                ws.send(json.dumps({
                    "type": "FEED_SETUP", "channel": 1, "acceptDataFormat": "FULL",
                    "acceptAggregationPeriod": 0.1,
                }))
            elif kind == "FEED_CONFIG" and message.get("channel") == 1:
                if message.get("dataFormat") != "FULL":
                    raise ValueError("Snapshot feed requires full-object events.")
                ready = True
                return ws
        raise ValueError("Snapshot handshake exceeded 25 seconds.")
    finally:
        if not ready:
            ws.close()


def get_underlying_snapshot(ws, symbol, session_guard=None):
    if session_guard:
        session_guard()
    ws.send(json.dumps({
        "type": "FEED_SUBSCRIPTION", "channel": 1,
        "add": [{"symbol": symbol, "type": kind} for kind in ("Trade", "Quote", "Summary")],
    }))
    result = dict(
        open=None, high=None, low=None, last=None, day_id=None,
        last_day_id=None, last_time=None, prev_close=None, prev_day_id=None,
        prev_close_type=None,
    )
    quote_mid = None
    start = time.time()
    while time.time() - start < 5:
        if session_guard:
            session_guard()
        try:
            ws.settimeout(0.5)
            message = json.loads(ws.recv())
        except WebSocketTimeoutException:
            continue
        if message.get("type") in {"ERROR", "CHANNEL_CLOSED"}:
            raise ValueError("Provider reported a snapshot feed error.")
        if message.get("type") != "FEED_DATA":
            continue
        for data in message.get("data", []):
            if data.get("eventSymbol") != symbol:
                continue
            kind = data.get("eventType")
            if kind == "Trade":
                price = finite_number(data.get("price"))
                if price is not None and price > 0:
                    result.update(last=price, last_day_id=data.get("dayId"), last_time=data.get("time"))
            elif kind == "Summary":
                result.update(
                    day_id=data.get("dayId"), prev_day_id=data.get("prevDayId"),
                    prev_close_type=data.get("prevDayClosePriceType"),
                )
                for field, source in (
                    ("open", "dayOpenPrice"), ("high", "dayHighPrice"),
                    ("low", "dayLowPrice"), ("prev_close", "prevDayClosePrice"),
                ):
                    value = finite_number(data.get(source))
                    result[field] = value if value is not None and value > 0 else None
            elif kind == "Quote":
                bid, ask = (finite_number(data.get(field)) for field in ("bidPrice", "askPrice"))
                if bid is not None and ask is not None and 0 < bid <= ask:
                    quote_mid = (bid + ask) / 2
        if result["last"] and all(result[field] is not None for field in ("open", "high", "low")):
            break
    result["price"] = result["last"] or quote_mid
    result["comparable"] = result["day_id"] is not None and result["day_id"] == result["last_day_id"]
    return result


def get_underlying_price(ws, symbol):
    return get_underlying_snapshot(ws, symbol)["price"]


def generate_option_symbols(center_price, option_prefix, expiration, strikes_up, strikes_down, increment):
    center = round(center_price / increment) * increment
    symbols = []
    for index in range(-strikes_down, strikes_up + 1):
        strike = center + index * increment
        if strike > 0:
            text = str(int(strike)) if strike == int(strike) else str(strike)
            symbols.extend(f".{option_prefix}{expiration}{side}{text}" for side in "CP")
    return symbols


def fetch_option_data(ws, symbols, wait_seconds=15, oi_symbols=None, session_guard=None):
    if session_guard:
        session_guard()
    subscriptions = [
        {"symbol": symbol, "type": kind}
        for symbol in symbols for kind in ("Greeks", "Summary", "Trade")
    ]
    subscriptions.extend(
        {"symbol": symbol, "type": "Summary"}
        for symbol in sorted(set(oi_symbols or ()) - set(symbols))
    )
    ws.send(json.dumps({"type": "FEED_SUBSCRIPTION", "channel": 1, "add": subscriptions}))
    data = {}
    start = time.time()
    last_keepalive = time.monotonic()
    while time.time() - start < wait_seconds:
        if session_guard:
            session_guard()
        if time.monotonic() - last_keepalive >= 15:
            ws.send(json.dumps({"type": "KEEPALIVE", "channel": 0}))
            last_keepalive = time.monotonic()
        try:
            ws.settimeout(0.5)
            message = json.loads(ws.recv())
        except WebSocketTimeoutException:
            continue
        if message.get("type") in {"ERROR", "CHANNEL_CLOSED"}:
            raise ValueError("Provider reported an option snapshot feed error.")
        if message.get("type") == "FEED_DATA":
            for item in message.get("data", []):
                symbol = item.get("eventSymbol")
                kind = item.get("eventType")
                values = data.setdefault(symbol, {})
                if kind == "Greeks":
                    values.update(gamma=item.get("gamma"), delta=item.get("delta"), iv=item.get("volatility"))
                elif kind == "Summary":
                    values["oi"] = item.get("openInterest")
                elif kind == "Trade":
                    values["volume"] = item.get("dayVolume")
    return data
