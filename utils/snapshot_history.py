"""Durable sampled wall history. No historical reconstruction or inferred missing values."""
import csv
import io
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from utils.exposure_profile import _nonnegative_number, build_exposure_profile, get_wall_levels
from utils.flow_store import NEW_YORK, finite_number
from utils.gex_calculator import parse_option_symbol
from utils.intraday_gex import gex_scope


HISTORY_DB = Path(__file__).resolve().parent.parent / "flow_data" / "snapshot_history.sqlite3"
HISTORY_CSV = HISTORY_DB.with_suffix(".csv")
DEFAULT_CONFIG = dict(
    enabled=True, symbol="SPX", root="SPXW", expiry=None,
    strikes_up=50, strikes_down=50, increment=5,
)
COLUMNS = [
    "SLNO", "TIMESTAMP", "CURRENT PRICE", "CALL WALL STRIKE", "CALL WALL STRENGTH",
    "PUT WALL STRIKE", "PUT WALL STRENGTH", "DAY OPEN", "DAY HIGH", "DAY LOW",
    "CLOSE PRICE", "REMARKS", "CALL WALL OI", "PUT WALL OI", "ABS GEX STRIKE",
    "ABS GEX VALUE", "NET GEX STRIKE", "NET GEX VALUE", "P/C RATIO FOR OI",
    "P/C RATIO FOR VOLUME", "CALL WALL CROSSED", "PUT WALL CROSSED",
    "SYMBOL", "OPTION ROOT", "EXPIRY", "SESSION DATE", "PRICE TIMESTAMP",
    "PROVIDER SESSION DATE", "PRICE SESSION DATE", "PREVIOUS CLOSE DATE",
    "PREVIOUS CLOSE TYPE", "STRIKES ABOVE", "STRIKES BELOW", "STRIKE INCREMENT",
    "RANGE LOW", "RANGE HIGH", "OI COVERAGE", "VOLUME COVERAGE", "GAMMA COVERAGE",
    "GEX UNITS", "OBSERVATION GAP SECONDS",
    "POSITIVE NET GEX STRIKE", "POSITIVE NET GEX VALUE",
    "NEGATIVE NET GEX STRIKE", "NEGATIVE NET GEX VALUE",
]


def validate_config(config):
    if type(config.get("enabled")) is not bool:
        raise ValueError("History enabled must be boolean.")
    for key in ("symbol", "root"):
        if not isinstance(config.get(key), str) or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", config[key]):
            raise ValueError("History symbol/root must be uppercase symbol identifiers.")
    for key in ("strikes_up", "strikes_down"):
        if type(config.get(key)) is not int or not 5 <= config[key] <= 50:
            raise ValueError("History strike counts must be integers from 5 through 50.")
    increment = finite_number(config.get("increment"))
    if increment is None or not 0.5 <= increment <= 100:
        raise ValueError("History strike increment must be between 0.5 and 100.")
    expiry = config.get("expiry")
    if expiry is not None:
        if not isinstance(expiry, str) or not re.fullmatch(r"\d{6}", expiry):
            raise ValueError("History expiry must be YYMMDD or automatic.")
        datetime.strptime(expiry, "%y%m%d")
    return {key: config[key] for key in DEFAULT_CONFIG}


def day_label(day_id):
    if type(day_id) is int:
        return (datetime(1970, 1, 1) + timedelta(days=day_id)).strftime("%m/%d/%Y")
    return None


def time_label(milliseconds):
    value = finite_number(milliseconds)
    return datetime.fromtimestamp(value / 1000, NEW_YORK).strftime("%m/%d/%Y %H:%M:%S %Z%z") if value and value > 0 else None


def confirmed_previous_close(prices):
    current, previous = prices.get("day_id"), prices.get("prev_day_id")
    value = finite_number(prices.get("prev_close"))
    if (
        type(current) is int and type(previous) is int and previous < current
        and prices.get("prev_close_type") == "FINAL" and value is not None and value > 0
    ):
        return value
    return None


def snapshot_metrics(option_data, spot, expected_symbols=None):
    symbols = list(expected_symbols if expected_symbols is not None else option_data)
    selected = {symbol: option_data.get(symbol, {}) for symbol in symbols}
    profile = build_exposure_profile(selected, spot)
    levels = get_wall_levels(profile, spot)
    result = {}
    contracts = [(parse_option_symbol(symbol), selected[symbol]) for symbol in symbols]
    contracts = [(contract, values) for contract, values in contracts if contract]
    for field, label in (("oi", "OI"), ("volume", "VOLUME"), ("gamma", "GAMMA")):
        count = sum(_nonnegative_number(values.get(field)) is not None for _, values in contracts)
        result[f"{label} COVERAGE"] = f"{count}/{len(symbols)}"
    for side in ("call", "put"):
        column = f"{side}_oi_gex"
        wall = levels[f"{side}_wall"]
        if profile.empty or profile[column].isna().any():
            wall = None
        label = side.upper()
        result[f"{label} WALL STRIKE"] = float(wall["strike"]) if wall else None
        result[f"{label} WALL STRENGTH"] = float(wall["gex"]) if wall else None
        observations = [
            _nonnegative_number(values.get("oi")) for contract, values in contracts
            if wall and contract["strike"] == wall["strike"]
            and contract["type"] == ("C" if side == "call" else "P")
        ]
        result[f"{label} WALL OI"] = sum(observations) if observations and None not in observations else None
    for field, label in (("oi", "OI"), ("volume", "VOLUME")):
        totals = {}
        for side in "CP":
            values = [_nonnegative_number(data.get(field)) for contract, data in contracts if contract["type"] == side]
            totals[side] = sum(values) if values and None not in values else None
        result[f"P/C RATIO FOR {label}"] = (
            totals["P"] / totals["C"] if totals["C"] is not None and totals["C"] > 0
            and totals["P"] is not None else None
        )
    complete = not profile.empty and not profile[["call_oi_gex", "put_oi_gex"]].isna().any().any()
    for label, values in (
        ("ABS", profile["call_oi_gex"] + profile["put_oi_gex"]),
        ("NET", profile["net_oi_gex"]),
        ("POSITIVE NET", profile["net_oi_gex"].where(profile["net_oi_gex"] > 0)),
        ("NEGATIVE NET", profile["net_oi_gex"].where(profile["net_oi_gex"] < 0)),
    ):
        result[f"{label} GEX STRIKE"] = result[f"{label} GEX VALUE"] = None
        if complete and values.abs().max() > 0:
            candidates = profile.loc[values.abs() == values.abs().max(), "strike"]
            strike = min(candidates, key=lambda value: (abs(value - spot), value))
            index = profile.index[profile["strike"] == strike][0]
            result[f"{label} GEX STRIKE"] = float(strike)
            result[f"{label} GEX VALUE"] = float(values.loc[index])
    result.update({
        "RANGE LOW": float(profile["strike"].min()) if not profile.empty else None,
        "RANGE HIGH": float(profile["strike"].max()) if not profile.empty else None,
        "GEX UNITS": "USD per 1% spot move; call-positive/put-negative convention",
    })
    return result


def make_row(config, expiry, session_date, prices, option_data, symbols, at_ms):
    spot = finite_number(prices.get("last"))
    if spot is None or spot <= 0:
        raise ValueError("History requires an observed underlying trade; fallback prices are not recorded.")
    metrics = snapshot_metrics(option_data, spot, symbols)
    close = confirmed_previous_close(prices)
    notes = []
    if close is None:
        notes.append("Previous official close unconfirmed; CLOSE PRICE left blank")
    if any(metrics[f"{field} COVERAGE"] != f"{len(symbols)}/{len(symbols)}" for field in ("OI", "VOLUME", "GAMMA")):
        notes.append("Incomplete inputs; affected metrics unavailable")
    event_ms = finite_number(prices.get("last_time"))
    if event_ms is None or event_ms <= 0:
        notes.append("Underlying trade timestamp unavailable")
    elif at_ms - event_ms > 120000:
        notes.append(f"Underlying last trade is {(at_ms - event_ms) / 60000:.1f} minutes old")
    return {
        **metrics, "TIMESTAMP": time_label(at_ms), "CURRENT PRICE": spot,
        "DAY OPEN": prices.get("open"), "DAY HIGH": prices.get("high"), "DAY LOW": prices.get("low"),
        "CLOSE PRICE": close, "REMARKS": "; ".join(notes),
        "SYMBOL": config["symbol"], "OPTION ROOT": config["root"],
        "EXPIRY": datetime.strptime(expiry, "%y%m%d").strftime("%m/%d/%Y"),
        "SESSION DATE": session_date.strftime("%m/%d/%Y"),
        "PRICE TIMESTAMP": time_label(event_ms), "PROVIDER SESSION DATE": day_label(prices.get("day_id")),
        "PRICE SESSION DATE": day_label(prices.get("last_day_id")),
        "PREVIOUS CLOSE DATE": day_label(prices.get("prev_day_id")),
        "PREVIOUS CLOSE TYPE": prices.get("prev_close_type"),
        "STRIKES ABOVE": config["strikes_up"], "STRIKES BELOW": config["strikes_down"],
        "STRIKE INCREMENT": config["increment"],
    }


def csv_text(rows):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows({column: row.get(column) for column in COLUMNS} for row in rows)
    return output.getvalue()


def capture_reason(remarks):
    reasons = [
        part.strip() for part in (remarks or "").split(";")
        if part.strip() in {"First collected snapshot", "15-minute capture"}
        or re.fullmatch(r"(Call|Put) wall changed .+ -> .+", part.strip())
    ]
    return "; ".join(reasons) or "See full details"


def crossing_history(rows):
    """Collapse repeated session annotations, keeping each scope/side/event distinct."""
    scope = [
        "SYMBOL", "OPTION ROOT", "EXPIRY", "SESSION DATE",
        "STRIKES ABOVE", "STRIKES BELOW", "STRIKE INCREMENT",
    ]
    columns = [*scope, "Side", "Wall strike", "Crossing (ET)"]
    events = []
    for row in sorted(rows, key=lambda item: item["SLNO"]):
        for side in ("CALL", "PUT"):
            for label in (row.get(f"{side} WALL CROSSED") or "").split(";"):
                if label.strip():
                    events.append({
                        **{key: row.get(key) for key in scope}, "Side": side.title(),
                        "Wall strike": row.get(f"{side} WALL STRIKE"), "Crossing (ET)": label.strip(),
                    })
    return pd.DataFrame(events, columns=columns).drop_duplicates(ignore_index=True)


def wall_strength_history(rows):
    """Per-strike side-specific strength extrema from retained snapshots, not continuous highs/lows."""
    columns = [
        "Side", "Wall strike", "Low strength", "High strength", "Latest valid strength",
        "Low captured (ET)", "High captured (ET)", "Latest valid capture (ET)", "Valid snapshots",
        "Wall status", "Latest snapshot (ET)",
    ]
    latest_row = max(rows, key=lambda row: row["SLNO"]) if rows else {}
    observations = []
    for row in rows:
        for side in ("CALL", "PUT"):
            strike = _nonnegative_number(row.get(f"{side} WALL STRIKE"))
            strength = _nonnegative_number(row.get(f"{side} WALL STRENGTH"))
            if not strike or strength is None:
                continue
            observations.append({
                "Side": side.title(), "Wall strike": strike, "Strength": strength,
                "Captured": row["TIMESTAMP"], "SLNO": row["SLNO"],
            })
    if not observations:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(observations).sort_values("SLNO", kind="stable")
    result = []
    for (side, strike), group in frame.groupby(["Side", "Wall strike"], sort=True):
        low = group.loc[group["Strength"].idxmin()]
        high = group.loc[group["Strength"].idxmax()]
        latest = group.iloc[-1]
        current_strike = _nonnegative_number(latest_row.get(f"{side.upper()} WALL STRIKE"))
        current_strength = _nonnegative_number(latest_row.get(f"{side.upper()} WALL STRENGTH"))
        if not current_strike:
            status = "Latest wall unavailable"
        elif strike != current_strike:
            status = "Former wall"
        elif current_strength is None:
            status = "Latest wall; strength unavailable"
        else:
            status = "Latest wall"
        result.append({
            "Side": side, "Wall strike": strike,
            "Low strength": low["Strength"], "High strength": high["Strength"],
            "Latest valid strength": latest["Strength"],
            "Low captured (ET)": low["Captured"], "High captured (ET)": high["Captured"],
            "Latest valid capture (ET)": latest["Captured"], "Valid snapshots": len(group),
            "Wall status": status, "Latest snapshot (ET)": latest_row.get("TIMESTAMP"),
        })
    return pd.DataFrame(result, columns=columns)


def history_style(frame):
    styles = frame.map(lambda _: "")
    for sign, color in (("POSITIVE", "#36cba5"), ("NEGATIVE", "#f45b76")):
        for suffix in ("STRIKE", "VALUE"):
            column = f"{sign} NET GEX {suffix}"
            if column in styles:
                styles[column] = f"color: {color}"
    for side, color in (("CALL", "#36cba5"), ("PUT", "#f45b76")):
        for suffix in ("WALL STRIKE", "WALL STRENGTH", "WALL OI", "WALL CROSSED"):
            column = f"{side} {suffix}"
            if column in styles:
                styles[column] = f"color: {color}"
        strike_column, crossed_column = f"{side} WALL STRIKE", f"{side} WALL CROSSED"
        if strike_column in styles and crossed_column in frame:
            crossed = frame[crossed_column].fillna("").ne("")
            styles.loc[crossed, strike_column] += "; font-weight: bold; background-color: #243343"
    return styles


class HistoryStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else HISTORY_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, session TEXT NOT NULL,
                at_ms INTEGER NOT NULL, symbol TEXT NOT NULL, root TEXT NOT NULL,
                expiry TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(scope, session, at_ms)
            );
            CREATE INDEX IF NOT EXISTS sample_selection ON samples(symbol, root, expiry, at_ms);
            CREATE TABLE IF NOT EXISTS gex_captures (
                scope TEXT NOT NULL, session TEXT NOT NULL, at_ms INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(scope, session, at_ms)
            );
            CREATE TABLE IF NOT EXISTS observations (scope TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crossings (
                id INTEGER PRIMARY KEY, scope TEXT NOT NULL, session TEXT NOT NULL,
                side TEXT NOT NULL, strike REAL NOT NULL, at_ms INTEGER NOT NULL,
                direction TEXT NOT NULL, from_ms INTEGER NOT NULL, from_price REAL NOT NULL,
                to_price REAL NOT NULL
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT payload FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["payload"]) if row else default

    def set(self, key, payload):
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                (key, json.dumps(payload, allow_nan=False)),
            )

    def observe(self, row, at_ms, price_ms):
        scope_values = [row[key] for key in (
            "SYMBOL", "OPTION ROOT", "EXPIRY", "STRIKES ABOVE", "STRIKES BELOW", "STRIKE INCREMENT",
        )]
        scope_values[-1] = float(scope_values[-1])
        scope = json.dumps(scope_values)
        session = row["SESSION DATE"]
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute("SELECT payload FROM observations WHERE scope=?", (scope,)).fetchone()
            state = json.loads(previous["payload"]) if previous else {}
            if state.get("session") != session:
                state = {}
            if state and at_ms <= state["at_ms"]:
                return False
            gap = (at_ms - state["at_ms"]) / 1000 if state else None
            row = dict(row, **{"OBSERVATION GAP SECONDS": gap})
            reasons = ["First collected snapshot"] if not state else []
            due = not state or at_ms >= state["next_due"]
            if state and due:
                reasons.append("15-minute capture")
            if gap is not None and gap > 120:
                reasons.append(f"Observation gap {gap:.0f}s; no backfill")
            walls = state.get("walls", {})
            seen = state.get("seen", {})
            new_crossings = 0
            price = row["CURRENT PRICE"]
            same_day = state.get("price_day") is not None and state.get("price_day") == row["PRICE SESSION DATE"]
            advancing = finite_number(price_ms) is not None and price_ms > state.get("price_ms", 0)
            comparable = (
                same_day and advancing and state.get("price_ms", 0) > 0
            )
            for side in ("CALL", "PUT"):
                tracked = seen.setdefault(side, {})
                for text, baseline in tracked.items():
                    strike = float(text)
                    position = (price > strike) - (price < strike)
                    if comparable and position and baseline["position"] * position < 0:
                        self.db.execute(
                            "INSERT INTO crossings(scope,session,side,strike,at_ms,direction,from_ms,from_price,to_price) "
                            "VALUES(?,?,?,?,?,?,?,?,?)",
                            (scope, session, side, strike, at_ms, "UP" if position > 0 else "DOWN",
                             baseline["at_ms"], baseline["price"], price),
                        )
                        new_crossings += 1
                    if position and (advancing or not same_day):
                        tracked[text] = dict(position=position, price=price, at_ms=at_ms)
                    elif not same_day:
                        tracked[text] = dict(position=position, price=price, at_ms=at_ms)
                strike = row.get(f"{side} WALL STRIKE")
                if strike is not None:
                    if state and walls.get(side) != strike:
                        reasons.append(f"{side.title()} wall changed {walls.get(side)} -> {strike:g}")
                    walls[side] = strike
                    tracked.setdefault(str(strike), dict(
                        position=(price > strike) - (price < strike), price=price, at_ms=at_ms,
                    ))
            changed = any("wall changed" in reason for reason in reasons)
            record = due or changed
            if record:
                row["REMARKS"] = "; ".join(filter(None, [
                    *reasons, row.get("REMARKS"),
                    f"{new_crossings} sampled crossing(s)" if new_crossings else None,
                ]))
                self.db.execute(
                    "INSERT INTO samples(scope,session,at_ms,symbol,root,expiry,payload) VALUES(?,?,?,?,?,?,?)",
                    (scope, session, at_ms, row["SYMBOL"], row["OPTION ROOT"], row["EXPIRY"],
                     json.dumps(row, allow_nan=False)),
                )
            state = dict(
                session=session, at_ms=at_ms, next_due=at_ms + 900000 if due else state["next_due"],
                walls=walls, seen=seen, price_day=row["PRICE SESSION DATE"],
                price_ms=price_ms if finite_number(price_ms) is not None else 0,
            )
            self.db.execute(
                "INSERT INTO observations VALUES(?,?) ON CONFLICT(scope) DO UPDATE SET payload=excluded.payload",
                (scope, json.dumps(state, allow_nan=False)),
            )
        return record or bool(new_crossings)

    def save_gex_capture(self, capture):
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO gex_captures VALUES(?,?,?,?) ON CONFLICT DO NOTHING",
                (capture["scope"], capture["session"], capture["at_ms"], json.dumps(capture, allow_nan=False)),
            )
        return cursor.rowcount == 1

    def gex_sessions(self, config, expiry):
        return [
            row["session"] for row in self.db.execute(
                "SELECT DISTINCT session FROM gex_captures WHERE scope=? ORDER BY session DESC",
                (gex_scope(config, expiry),),
            )
        ]

    def gex_rows(self, config, expiry, session):
        return [
            json.loads(row["payload"]) for row in self.db.execute(
                "SELECT payload FROM gex_captures WHERE scope=? AND session=? ORDER BY at_ms",
                (gex_scope(config, expiry), session),
            )
        ]

    def rows(self, symbol=None, root=None, expiry=None, session=None):
        conditions, parameters = [], []
        for column, value in (("symbol", symbol), ("root", root), ("expiry", expiry), ("session", session)):
            if value is not None:
                conditions.append(f"{column}=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.db:
            records = self.db.execute("SELECT * FROM samples" + where + " ORDER BY id", parameters).fetchall()
            crossings = {}
            for event in self.db.execute("SELECT * FROM crossings ORDER BY at_ms,id"):
                key = (event["scope"], event["session"], event["side"], event["strike"])
                crossings.setdefault(key, []).append(event)
        result = []
        for record in records:
            row = dict(json.loads(record["payload"]), SLNO=record["id"])
            marks = []
            for side in ("CALL", "PUT"):
                events = crossings.get((record["scope"], record["session"], side, row.get(f"{side} WALL STRIKE")), [])
                labels = [
                    f"{event['direction']} {time_label(event['at_ms'])} (sampled)"
                    for event in events
                ]
                row[f"{side} WALL CROSSED"] = "; ".join(labels)
                if labels:
                    marks.append(f"{side.title()} wall {row[f'{side} WALL STRIKE']:g} crossed: " + "; ".join(labels))
            row["REMARKS"] = "; ".join(filter(None, [row["REMARKS"], *marks]))
            result.append({column: row.get(column) for column in COLUMNS})
        return result

    def export(self, destination=None):
        destination = Path(destination) if destination is not None else self.path.with_suffix(".csv")
        text = csv_text(self.rows())
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="", dir=destination.parent, suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            os.replace(temporary, destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
