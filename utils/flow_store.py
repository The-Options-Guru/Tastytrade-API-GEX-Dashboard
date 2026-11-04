"""Local observed trade tape. Never infer missing history from snapshot totals."""
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime


FLOW_DB = Path(__file__).resolve().parent.parent / "flow_data" / "recorder.sqlite3"
NEW_YORK = ZoneInfo("America/New_York")


def finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


class FlowStore:
    def __init__(self, path=FLOW_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY, underlying TEXT NOT NULL, expiry TEXT NOT NULL,
                started_ms INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL,
                ended_ms INTEGER, reason TEXT, contract_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS contracts (
                symbol TEXT PRIMARY KEY, underlying TEXT NOT NULL, expiry TEXT NOT NULL,
                side TEXT NOT NULL, multiplier REAL NOT NULL, subscribed_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prints (
                symbol TEXT NOT NULL, idx TEXT NOT NULL, connection_id INTEGER NOT NULL,
                time_ms INTEGER NOT NULL, premium REAL, signed_premium REAL,
                reason TEXT, action TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(symbol, idx)
            );
            CREATE TABLE IF NOT EXISTS updates (
                id INTEGER PRIMARY KEY, connection_id INTEGER NOT NULL,
                received_ms INTEGER NOT NULL, symbol TEXT NOT NULL, idx TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candles (
                underlying TEXT NOT NULL, session_date TEXT NOT NULL,
                time_ms INTEGER NOT NULL, close REAL NOT NULL,
                PRIMARY KEY(underlying, time_ms)
            );
            CREATE TABLE IF NOT EXISTS status (
                id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS prints_connection ON prints(connection_id, time_ms);
        """)

    def close(self):
        self.db.close()

    def recover(self):
        with self.db:
            self.db.execute(
                "UPDATE connections SET ended_ms=last_seen_ms, reason='Recorder restarted' "
                "WHERE ended_ms IS NULL"
            )

    def set_status(self, **status):
        with self.db:
            self.db.execute(
                "INSERT INTO status VALUES(1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (json.dumps(status),),
            )

    def begin(self, underlying, expiry, contracts, now_ms):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO connections(underlying,expiry,started_ms,last_seen_ms,contract_count) "
                "VALUES(?,?,?,?,?)",
                (underlying, expiry, now_ms, now_ms, len(contracts)),
            )
            self.add_contracts(underlying, expiry, contracts, now_ms)
        return cursor.lastrowid

    def add_contracts(self, underlying, expiry, contracts, now_ms):
        self.db.executemany(
            "INSERT INTO contracts VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO NOTHING",
            [(symbol, underlying, expiry, contract["side"], contract["multiplier"], now_ms)
             for symbol, contract in contracts.items()],
        )

    def heartbeat(self, connection_id, now_ms, contract_count):
        with self.db:
            self.db.execute(
                "UPDATE connections SET last_seen_ms=?,contract_count=? WHERE id=?",
                (now_ms, contract_count, connection_id),
            )

    def end(self, connection_id, now_ms, reason):
        with self.db:
            self.db.execute(
                "UPDATE connections SET ended_ms=?,last_seen_ms=?,reason=? WHERE id=?",
                (now_ms, now_ms, reason, connection_id),
            )

    def apply_events(self, connection_id, events, received_ms):
        connection = self.db.execute("SELECT * FROM connections WHERE id=?", (connection_id,)).fetchone()
        if not connection:
            raise ValueError("Trade batch has no recording connection.")
        with self.db:
            for event in events:
                event_type = event.get("eventType")
                symbol = event.get("eventSymbol", "")
                if event_type == "Candle" and symbol.split("{", 1)[0] == connection["underlying"]:
                    close, timestamp = finite_number(event.get("close")), finite_number(event.get("time"))
                    if close is not None and close > 0 and timestamp and not int(event.get("eventFlags", 0)) & 2:
                        session_date = datetime.fromtimestamp(timestamp / 1000, NEW_YORK).date().isoformat()
                        self.db.execute(
                            "INSERT INTO candles VALUES(?,?,?,?) ON CONFLICT(underlying,time_ms) "
                            "DO UPDATE SET close=excluded.close",
                            (connection["underlying"], session_date, int(timestamp), close),
                        )
                    continue
                if event_type != "TimeAndSale":
                    continue
                contract = self.db.execute("SELECT * FROM contracts WHERE symbol=?", (symbol,)).fetchone()
                if not contract or contract["expiry"] != connection["expiry"]:
                    raise ValueError("Received a trade outside the subscribed expiry.")
                idx = event.get("index")
                timestamp = finite_number(event.get("time"))
                if idx is None or isinstance(idx, bool) or timestamp is None or timestamp <= 0:
                    raise ValueError("Trade has no valid index or timestamp.")
                payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
                fingerprint = hashlib.sha256(payload.encode()).hexdigest()
                previous = self.db.execute(
                    "SELECT * FROM prints WHERE symbol=? AND idx=?", (symbol, str(idx)),
                ).fetchone()
                window_start = connection["started_ms"]
                if previous:
                    window_start = self.db.execute(
                        "SELECT started_ms FROM connections WHERE id=?", (previous["connection_id"],)
                    ).fetchone()["started_ms"]
                if previous and previous["fingerprint"] == fingerprint:
                    continue
                if int(event.get("eventFlags", 0)) != 0:
                    raise ValueError("Unexpected transactional flags on the live-only trade subscription.")
                action = event.get("type")
                if action not in ("NEW", "CORRECTION", "CANCEL"):
                    raise ValueError("Unrecognized trade action.")
                premium = signed = None
                reason = None
                price, size = finite_number(event.get("price")), finite_number(event.get("size"))
                if action == "CANCEL":
                    reason = "cancelled"
                elif action == "CORRECTION" and (previous is None or previous["reason"] == "unmatched correction"):
                    reason = "unmatched correction"
                elif timestamp < max(window_start, contract["subscribed_ms"]):
                    reason = "before recording window"
                elif event.get("validTick") is not True:
                    reason = "invalid tick"
                elif event.get("spreadLeg") is not False:
                    reason = "spread leg or unknown spread status"
                elif price is None or size is None or price <= 0 or size <= 0:
                    reason = "invalid price or size"
                else:
                    premium = price * size * contract["multiplier"]
                    if not math.isfinite(premium):
                        raise ValueError("Trade premium overflow.")
                    side = event.get("aggressorSide")
                    if side not in ("BUY", "SELL"):
                        reason = "unclassified aggressor"
                    else:
                        sign = 1 if (contract["side"] == "call") == (side == "BUY") else -1
                        signed = sign * premium
                self.db.execute(
                    "INSERT INTO updates(connection_id,received_ms,symbol,idx,payload) VALUES(?,?,?,?,?)",
                    (connection_id, received_ms, symbol, str(idx), payload),
                )
                self.db.execute(
                    "INSERT INTO prints VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,idx) "
                    "DO UPDATE SET time_ms=excluded.time_ms,premium=excluded.premium,"
                    "signed_premium=excluded.signed_premium,reason=excluded.reason,"
                    "action=excluded.action,fingerprint=excluded.fingerprint",
                    (symbol, str(idx), connection_id, int(timestamp), premium, signed, reason, action, fingerprint),
                )


def read_flow_snapshot(path, underlying, expiry):
    path = Path(path)
    if not path.exists():
        return {"status": {}, "trades": [], "candles": [], "intervals": [], "minutes": [], "counts": []}
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        status = db.execute("SELECT payload FROM status WHERE id=1").fetchone()
        intervals = db.execute(
            "SELECT * FROM connections WHERE underlying=? AND expiry=? ORDER BY started_ms",
            (underlying, expiry),
        ).fetchall()
        scope = (
            " FROM prints p JOIN contracts c ON p.symbol=c.symbol "
            "WHERE c.underlying=? AND c.expiry=? "
        )
        trades = db.execute(
            "SELECT p.*" + scope + "AND p.signed_premium IS NOT NULL "
            "ORDER BY p.premium DESC,p.time_ms,p.idx LIMIT 200",
            (underlying, expiry),
        ).fetchall()
        minutes = db.execute(
            "SELECT p.connection_id, (p.time_ms / 60000)*60000 AS time_ms, "
            "SUM(p.signed_premium) AS signed_premium" + scope +
            "AND p.signed_premium IS NOT NULL GROUP BY p.connection_id,p.time_ms / 60000 "
            "ORDER BY time_ms,p.connection_id",
            (underlying, expiry),
        ).fetchall()
        counts = db.execute(
            "SELECT p.reason,COUNT(*) AS count" + scope + "GROUP BY p.reason",
            (underlying, expiry),
        ).fetchall()
        session_date = datetime.strptime(expiry, "%y%m%d").date().isoformat()
        candles = db.execute(
            "SELECT time_ms,close FROM candles WHERE underlying=? AND session_date=? ORDER BY time_ms",
            (underlying, session_date),
        ).fetchall()
    return {
        "status": json.loads(status["payload"]) if status else {},
        "trades": [dict(row) for row in trades],
        "candles": [dict(row) for row in candles],
        "intervals": [dict(row) for row in intervals],
        "minutes": [dict(row) for row in minutes],
        "counts": [dict(row) for row in counts],
    }


def read_recorder_status(path=FLOW_DB):
    path = Path(path)
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        row = db.execute("SELECT payload FROM status WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {}
