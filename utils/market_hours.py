"""Scheduled options sessions, not live halt detection or a feed-entitlement guarantee."""
import csv
import io
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, time as clock, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")
CALENDAR_URL = "https://www.cboe.com/us/options/holidays/csv/"
CACHE = Path(__file__).resolve().parent.parent / "flow_data" / "market_calendar.json"
# Cboe launch confirmation 10/19/2026, not merely the earlier proposed eligibility list:
# https://ir.cboe.com/news/news-details/2026/Cboe-Launches-Extended-Trading-Sessions-for-Select-Single-Stock-and-ETF-Options/default.aspx
EARLY_OPTIONS = frozenset(
    "AAPL AMD AMZN AVGO BABA BAC F GLD GOOG GOOGL HOOD INTC IWM META MRVL MSFT "
    "MU NFLX NOK NVDA ORCL PFE PLTR QQQ SNDK SPY TSLA TSM XLF".split()
)
LATE_OPTIONS = frozenset("NDX SPY QQQ IWM DIA GLD XLF".split())
GTH_PATTERN = re.compile(
    r"(\d{1,2}:\d{2} [AP]M) \((Mon|Tue|Wed|Thu|Fri|Sat|Sun)\) to "
    r"(\d{1,2}:\d{2} [AP]M) \((Mon|Tue|Wed|Thu|Fri|Sat|Sun)\)"
)
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class MarketState:
    allowed: bool
    label: str
    session_date: date | None = None
    next_open: datetime | None = None


def parse_calendar(text):
    rows = list(csv.DictReader(line for line in io.StringIO(text) if not line.startswith("#")))
    holidays = {}
    for row in rows:
        day = date.fromisoformat(row["Date"])
        rth = row["Regular Trading Hours"]
        hours = None
        if rth != "None":
            parts = rth.split(" - ")
            if len(parts) != 2:
                raise ValueError("Unrecognized Cboe regular-hours format.")
            hours = tuple(clock.fromisoformat(part) for part in parts)
        gth = row["Global Trading Hours"]
        if gth != "None" and " and ".join(match.group() for match in GTH_PATTERN.finditer(gth)) != gth:
            raise ValueError("Unrecognized Cboe overnight holiday format.")
        if day in holidays:
            raise ValueError("Duplicate holiday in exchange calendar.")
        holidays[day] = (hours, gth)
    years = {day.year for day in holidays}
    if not years or any(sum(day.year == year for day in holidays) < 8 for year in years):
        raise ValueError("Exchange holiday coverage is not established.")
    return years, holidays


@lru_cache(maxsize=2)
def load_calendar(bucket):
    current_year = datetime.now(ET).year
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        years, _ = parse_calendar(cached["csv"])
        if current_year in years and 0 <= time.time() - cached["fetched_at"] < 21600:
            return cached["csv"]
    response = requests.get(CALENDAR_URL, timeout=10)
    response.raise_for_status()
    text = response.text
    years, _ = parse_calendar(text)
    if current_year not in years:
        raise ValueError("The published exchange calendar does not cover the current year.")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump({"fetched_at": time.time(), "source": CALENDAR_URL, "csv": text}, file)
        os.replace(temporary, CACHE)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return text


def market_state(symbol, option_root, expiration=None, now=None, calendar_text=None):
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        raise ValueError("Market scheduling requires an aware timestamp.")
    now = now.astimezone(ET)
    if symbol not in EARLY_OPTIONS | LATE_OPTIONS | {"SPX"}:
        return MarketState(False, f"No verified options-session schedule for {symbol}; automatic fetching paused.")
    if symbol == "SPX" and option_root != "SPXW":
        return MarketState(False, "Only the SPXW PM-expiry schedule is verified; automatic fetching paused.")
    if (symbol == "NDX" and option_root != "NDXP") or (symbol not in {"SPX", "NDX"} and option_root != symbol):
        return MarketState(False, "No verified schedule for this option root; automatic fetching paused.")
    text = calendar_text if calendar_text is not None else load_calendar(int(time.time() // 21600))
    years, holidays = parse_calendar(text)
    if now.year not in years:
        raise ValueError("No verified exchange calendar for this year.")

    def regular(day):
        if day.year not in years or day.weekday() >= 5:
            return None
        return holidays[day][0] if day in holidays else (clock(9, 30), clock(16))

    def trade_date(day):
        for offset in range(10):
            candidate = day + timedelta(days=offset)
            if regular(candidate) is not None:
                return candidate
        return None

    def at(day, value):
        return datetime.combine(day, value, ET)

    expiry = datetime.strptime(expiration, "%y%m%d").date() if expiration else None
    if expiry:
        hours = regular(expiry)
        if hours is None:
            return MarketState(False, "Selected expiry has no verified regular trading session.")
        cutoff = hours[1]
        if option_root != "NDXP" and (
            symbol in LATE_OPTIONS or (symbol in EARLY_OPTIONS and expiry >= date(2026, 10, 19))
        ):
            cutoff = (at(expiry, cutoff) + timedelta(minutes=15)).time()
        if expiry < now.date() or now >= at(expiry, cutoff):
            return MarketState(False, "Selected expiry has stopped trading; select an active expiry.")

    windows = []
    for offset in range(-1, 11):
        day = now.date() + timedelta(days=offset)
        hours = regular(day)
        if hours is None:
            continue
        is_short = day in holidays
        end = hours[1]
        if symbol == "SPX":
            end = (at(day, end) + timedelta(minutes=15)).time() if is_short else clock(17)
        elif symbol in LATE_OPTIONS or (symbol in EARLY_OPTIONS and day >= date(2026, 10, 19)):
            end = (at(day, end) + timedelta(minutes=15)).time()
        windows.append((at(day, hours[0]), at(day, end), "RTH / supported late session", day))
        if symbol == "SPX":
            windows.append((at(day - timedelta(days=1), clock(20, 15)), at(day, clock(9, 25)), "SPX global session", day))
        elif symbol in EARLY_OPTIONS and day >= date(2026, 10, 19):
            windows.append((at(day, clock(7, 30)), at(day, clock(9, 25)), "Options pre-market", day))

    if symbol == "SPX":
        for day, (_, gth) in holidays.items():
            if abs((day - now.date()).days) > 10:
                continue
            for match in GTH_PATTERN.finditer(gth):
                start_time, start_weekday, end_time, end_weekday = match.groups()
                end_delta = (WEEKDAYS.index(end_weekday) - day.weekday() + 3) % 7 - 3
                end_day = day + timedelta(days=end_delta)
                start_day = end_day - timedelta(
                    days=(WEEKDAYS.index(end_weekday) - WEEKDAYS.index(start_weekday)) % 7
                )
                windows.append((
                    at(start_day, datetime.strptime(start_time, "%I:%M %p").time()),
                    at(end_day, datetime.strptime(end_time, "%I:%M %p").time()),
                    "SPX holiday global session", trade_date(end_day),
                ))

    windows.sort()
    for start, end, label, session_day in windows:
        if session_day is None:
            continue
        if start <= now < end:
            # During the late session the expiring PM series has already stopped.
            if symbol in {"SPX", "NDX"} and now >= at(session_day, regular(session_day)[1]):
                session_day = trade_date(session_day + timedelta(days=1))
                if session_day is None:
                    return MarketState(False, "Next PM expiry is outside verified calendar coverage.")
            if expiry and expiry < session_day:
                return MarketState(False, "Selected expiry predates the active trading session.")
            return MarketState(True, label, session_day)
    for start, _, _, session_day in windows:
        if start > now and session_day and (expiry is None or session_day <= expiry):
            return MarketState(False, "Outside scheduled options trading hours", session_day, start)
    return MarketState(False, "Closed; next session is outside verified calendar coverage.")
