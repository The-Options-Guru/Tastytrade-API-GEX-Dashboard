import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from utils.market_hours import ET, market_state, parse_calendar


# Retrieved from Cboe's 2026 options holiday CSV; includes holiday GTH, not just NYSE closures.
CALENDAR = (Path(__file__).parent / "fixtures" / "cboe_2026.csv").read_text(encoding="utf-8")


class MarketHoursTests(unittest.TestCase):
    def test_multi_year_csv_preserves_existing_sessions_and_requires_each_year_coverage(self):
        extra = "\n".join(f"Synthetic holiday,2027-01-{day:02d},None,None" for day in range(1, 9))
        combined = CALENDAR.rstrip() + "\n" + extra + "\n"
        self.assertEqual(parse_calendar(combined)[0], {2026, 2027})
        for moment in ("2026-11-04T10:00:00", "2026-11-26T12:00:00", "2026-11-27T13:15:00"):
            now = datetime.fromisoformat(moment).replace(tzinfo=ET)
            self.assertEqual(
                market_state("SPX", "SPXW", now=now, calendar_text=CALENDAR),
                market_state("SPX", "SPXW", now=now, calendar_text=combined),
            )
        self.assertTrue(market_state(
            "SPX", "SPXW", now=datetime(2027, 1, 11, 10, tzinfo=ET), calendar_text=combined,
        ).allowed)
        with self.assertRaises(ValueError):
            parse_calendar(CALENDAR.rstrip() + "\nSynthetic holiday,2027-01-01,None,None\n")

    def state(self, moment, symbol="SPX", root="SPXW", expiry=None):
        return market_state(
            symbol, root, expiry, now=datetime.fromisoformat(moment).replace(tzinfo=ET), calendar_text=CALENDAR,
        )

    def test_normal_spx_boundaries_and_nightly_breaks(self):
        for moment, expected in (
            ("2026-10-22T09:24:59", True), ("2026-10-22T09:25:00", False),
            ("2026-10-22T09:29:59", False), ("2026-10-22T09:30:00", True),
            ("2026-10-22T16:59:59", True), ("2026-10-22T17:00:00", False),
            ("2026-10-22T20:14:59", False), ("2026-10-22T20:15:00", True),
        ):
            with self.subTest(moment=moment):
                self.assertEqual(self.state(moment).allowed, expected)

    def test_overnight_and_late_session_use_next_active_pm_expiry(self):
        for moment in ("2026-10-22T16:00:00", "2026-10-22T20:15:00", "2026-10-23T00:01:00"):
            with self.subTest(moment=moment):
                state = self.state(moment)
                self.assertTrue(state.allowed)
                self.assertEqual(state.session_date, date(2026, 10, 23))
        self.assertFalse(self.state("2026-10-22T20:15:00", expiry="261022").allowed)
        self.assertTrue(self.state("2026-10-22T20:15:00", expiry="261023").allowed)

    def test_weekend_and_sunday_open(self):
        self.assertFalse(self.state("2026-10-23T20:15:00").allowed)
        self.assertFalse(self.state("2026-10-24T12:00:00").allowed)
        self.assertFalse(self.state("2026-10-25T20:14:59").allowed)
        sunday = self.state("2026-10-25T20:15:00")
        self.assertTrue(sunday.allowed)
        self.assertEqual(sunday.session_date, date(2026, 10, 26))

    def test_holiday_gth_remains_open_until_official_1130_cutoff(self):
        monday = self.state("2026-01-19T11:29:59")
        self.assertTrue(monday.allowed)
        self.assertEqual(monday.session_date, date(2026, 1, 20))
        closed = self.state("2026-01-19T11:30:00")
        self.assertFalse(closed.allowed)
        self.assertEqual(closed.next_open.hour, 20)
        self.assertTrue(self.state("2026-01-19T20:15:00").allowed)
        self.assertFalse(self.state("2026-01-19T10:00:00", "SPY", "SPY").allowed)

    def test_good_friday_and_christmas_have_no_overnight_session(self):
        for moment in ("2026-04-02T20:15:00", "2026-04-03T10:00:00", "2026-12-24T20:15:00", "2026-12-25T10:00:00"):
            with self.subTest(moment=moment):
                self.assertFalse(self.state(moment).allowed)

    def test_early_close_no_afternoon_curb_and_expiring_series_stops_at_1300(self):
        self.assertTrue(self.state("2026-11-27T12:59:59", expiry="261127").allowed)
        self.assertFalse(self.state("2026-11-27T13:00:00", expiry="261127").allowed)
        self.assertTrue(self.state("2026-11-27T13:14:59", expiry="261130").allowed)
        self.assertFalse(self.state("2026-11-27T13:15:00", expiry="261130").allowed)
        self.assertFalse(self.state("2026-11-27T16:30:00").allowed)

    def test_launched_equity_morning_sessions_only(self):
        self.assertTrue(self.state("2026-10-22T07:30:00", "AAPL", "AAPL").allowed)
        self.assertFalse(self.state("2026-10-22T09:25:00", "AAPL", "AAPL").allowed)
        self.assertTrue(self.state("2026-10-22T16:14:59", "AAPL", "AAPL").allowed)
        self.assertFalse(self.state("2026-10-22T16:15:00", "AAPL", "AAPL").allowed)
        self.assertTrue(self.state("2026-10-22T16:14:59", "AAPL", "AAPL", "261022").allowed)
        self.assertFalse(self.state("2026-10-22T16:15:00", "AAPL", "AAPL", "261022").allowed)
        self.assertFalse(self.state("2026-10-16T08:00:00", "AAPL", "AAPL").allowed)
        self.assertFalse(self.state("2026-10-22T08:00:00", "DIA", "DIA").allowed)
        self.assertTrue(self.state("2026-10-22T08:00:00", "SPY", "SPY").allowed)
        self.assertFalse(self.state("2026-10-22T06:00:00", "SPY", "SPY").allowed)

    def test_ndx_proposed_overnight_not_enabled_and_expiry_stops_at_1600(self):
        self.assertFalse(self.state("2026-10-22T22:00:00", "NDX", "NDXP").allowed)
        self.assertTrue(self.state("2026-10-22T15:59:59", "NDX", "NDXP", "261022").allowed)
        self.assertFalse(self.state("2026-10-22T16:00:00", "NDX", "NDXP", "261022").allowed)
        self.assertTrue(self.state("2026-10-22T16:14:59", "NDX", "NDXP", "261023").allowed)
        self.assertFalse(self.state("2026-10-22T16:15:00", "NDX", "NDXP", "261023").allowed)

    def test_dst_is_exchange_local_not_fixed_utc(self):
        for utc_hour, local_date in ((0, "2026-10-22"), (1, "2026-11-05")):
            # 20:15 ET is 00:15 UTC in October, 01:15 UTC after the November DST change.
            day = date.fromisoformat(local_date)
            stamp = datetime(day.year, day.month, day.day + 1, utc_hour, 15, tzinfo=timezone.utc)
            self.assertTrue(market_state("SPX", "SPXW", now=stamp, calendar_text=CALENDAR).allowed)

    def test_unknown_calendar_year_and_naive_timestamp_fail_closed(self):
        with self.assertRaises(ValueError):
            self.state("2027-01-04T10:00:00")
        with self.assertRaises(ValueError):
            market_state("SPX", "SPXW", now=datetime(2026, 10, 22), calendar_text=CALENDAR)
        for text in ("bad", CALENDAR.replace("8:15 PM (Thu)", "unknown")):
            with self.assertRaises((ValueError, KeyError)):
                parse_calendar(text)
        self.assertFalse(self.state("2026-12-31T20:15:00").allowed)

    def test_unsupported_symbol_root_does_not_request_calendar(self):
        with patch("utils.market_hours.load_calendar", side_effect=AssertionError("No requests")):
            for symbol, root in (("UNKNOWN", "UNKNOWN"), ("SPX", "SPX"), ("NDX", "NDX"), ("AAPL", "BAD")):
                self.assertFalse(market_state(symbol, root).allowed)


if __name__ == "__main__":
    unittest.main()
