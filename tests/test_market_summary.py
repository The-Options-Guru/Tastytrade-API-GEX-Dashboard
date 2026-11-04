import json
import math
import random
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
from streamlit.testing.v1 import AppTest

from simple_dashboard import build_gex_bar_chart, fetch_option_data, get_underlying_snapshot, market_sentiment
from utils.flow_store import NEW_YORK
from utils.exposure_profile import (
    build_exposure_profile, calculate_max_pain, get_wall_bias, get_wall_levels, get_open_to_wall_context,
)
from utils.option_chain import fetch_expiry_chain


class MarketSentimentTests(unittest.TestCase):
    now = int(datetime(2026, 10, 22, 10, tzinfo=NEW_YORK).timestamp() * 1000)

    def prices(self, **changes):
        return dict(dict(last=101, open=100, last_time=self.now, day_id=20748, last_day_id=20748), **changes)

    def test_bull_bear_and_neutral_with_identical_freshness_control(self):
        for last, expected in ((101, "Bullish"), (99, "Bearish"), (100, "Neutral")):
            result = market_sentiment(self.prices(last=last), now_ms=self.now)
            self.assertEqual(result["label"], expected)
            self.assertEqual(result["change"], last - 100)
            self.assertEqual(result["percent"], last - 100)
            self.assertEqual(result["reason"], "")

    def test_freshness_boundary_stale_future_and_previous_day(self):
        for age, expected in ((120000, "Bullish"), (120001, "Unavailable"), (-1, "Unavailable"), (86400000, "Unavailable")):
            result = market_sentiment(self.prices(last_time=self.now - age), now_ms=self.now)
            self.assertEqual(result["label"], expected)
        for changes in (dict(day_id=20747), dict(last_day_id=20747), dict(day_id=None), dict(day_id=True)):
            self.assertEqual(market_sentiment(self.prices(**changes), now_ms=self.now)["label"], "Unavailable")
        midnight = int(datetime(2026, 10, 23, 0, 0, 30, tzinfo=NEW_YORK).timestamp() * 1000)
        self.assertEqual(market_sentiment(self.prices(
            day_id=20749, last_day_id=20749, last_time=midnight - 60000,
        ), now_ms=midnight)["label"], "Unavailable")

    def test_invalid_missing_quote_only_and_fallback_inputs(self):
        for field in ("last", "open", "last_time"):
            for bad in (None, True, 0, -1, float("nan"), float("inf"), "bad"):
                with self.subTest(field=field, bad=bad):
                    self.assertEqual(market_sentiment(self.prices(**{field: bad}), now_ms=self.now)["label"], "Unavailable")
        self.assertEqual(market_sentiment(self.prices(), True, self.now)["label"], "Unavailable")
        self.assertEqual(market_sentiment({"price": 101, "open": 100}, now_ms=self.now)["label"], "Unavailable")

    def test_panel_exposes_basis_timestamp_and_stale_reason(self):
        app = AppTest.from_string("""
from simple_dashboard import render_market_sentiment
render_market_sentiment("SPX", {"last": 101, "open": 100, "last_time": 1792677600000,
                               "day_id": 20748, "last_day_id": 20748})
""")
        with patch("simple_dashboard.time.time", return_value=self.now / 1000):
            app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].label, "SPX market sentiment")
        self.assertEqual(app.metric[0].value, "Bullish")
        self.assertTrue(any("Price timestamp: 10/22/2026 10:00:00" in item.value for item in app.caption))
        self.assertTrue(any("not a forecast" in item.value for item in app.caption))
        with patch("simple_dashboard.time.time", return_value=self.now / 1000 + 121):
            app.run(timeout=30)
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value, "Unavailable")
        self.assertTrue(any("stale" in item.value for item in app.caption))


class MaxPainTests(unittest.TestCase):
    def make_chain(self, strikes, multiplier=100):
        return {
            f".SPY261022{option_type}{strike}": {
                "strike": strike, "side": side, "multiplier": multiplier,
            }
            for strike in strikes for side, option_type in (("call", "C"), ("put", "P"))
        }

    def test_prefix_sum_matches_independent_brute_force_payout(self):
        rng = random.Random(42)
        for _ in range(20):
            contracts = self.make_chain([90, 95, 100, 100.5, 105, 110], multiplier=50)
            data = {symbol: {"oi": rng.randrange(30)} for symbol in contracts}
            payouts = {
                price: sum(
                    data[symbol]["oi"] * contract["multiplier"] * max(
                        price - contract["strike"] if contract["side"] == "call"
                        else contract["strike"] - price, 0,
                    )
                    for symbol, contract in contracts.items()
                )
                for price in (90, 95, 100, 100.5, 105, 110)
            }
            actual = calculate_max_pain(contracts, data)
            self.assertEqual(actual["payout"], min(payouts.values()))
            self.assertEqual(actual["strikes"], [
                strike for strike, payout in payouts.items() if payout == min(payouts.values())
            ])
            self.assertEqual(actual["observed"], len(contracts))

    def test_minimum_is_not_just_largest_oi_strike(self):
        chain = self.make_chain([90, 100, 110])
        data = {symbol: {"oi": 0} for symbol in chain}
        data[".SPY261022C90"]["oi"] = 10
        data[".SPY261022P110"]["oi"] = 10
        result = calculate_max_pain(chain, data)
        self.assertEqual(result["strikes"], [90, 100, 110])
        self.assertEqual(result["payout"], 20000)

    def test_zero_is_valid_but_all_zero_has_no_informative_level(self):
        chain = self.make_chain([90, 100])
        data = {symbol: {"oi": 0} for symbol in chain}
        result = calculate_max_pain(chain, data)
        self.assertEqual(result["observed"], 4)
        self.assertEqual(result["strikes"], [])
        self.assertIn("zero", result["reason"])

    def test_missing_invalid_or_nonfinite_oi_blocks_full_chain_result(self):
        chain = self.make_chain([90, 100])
        for bad in (None, "bad", -1, math.nan, math.inf, True):
            with self.subTest(bad=bad):
                data = {symbol: {"oi": 10} for symbol in chain}
                data[".SPY261022C90"]["oi"] = bad
                result = calculate_max_pain(chain, data)
                self.assertEqual(result["strikes"], [])
                self.assertIsNone(result["payout"])
                self.assertEqual((result["observed"], result["expected"]), (3, 4))
        self.assertEqual(calculate_max_pain({}, {})["strikes"], [])


class ChainListingTests(unittest.TestCase):
    def payload(self):
        strikes = [{
            "strike-price": "100.5",
            "call-streamer-symbol": ".SPXW261022C100.5",
            "put-streamer-symbol": ".SPXW261022P100.5",
        }]
        return {"data": {"items": [
            {"root-symbol": "SPX", "shares-per-contract": 100, "expirations": []},
            {"root-symbol": "SPXW", "shares-per-contract": 100, "expirations": [
                {"expiration-date": "2026-10-22", "strikes": strikes},
                {"expiration-date": "2026-10-23", "strikes": []},
            ]},
        ]}}

    def fetch(self, payload, prefix="SPXW", expiration="261022"):
        response = Mock()
        response.json.return_value = payload
        with patch("utils.option_chain.get_access_token", return_value="test-only"), patch(
            "utils.option_chain.requests.get", return_value=response,
        ) as get:
            result = fetch_expiry_chain("SPX", prefix, expiration)
        self.assertEqual(get.call_args.kwargs["timeout"], 20)
        response.raise_for_status.assert_called_once()
        return result

    def test_uses_listed_streamer_symbols_and_selected_root_and_expiry(self):
        contracts = self.fetch(self.payload())
        self.assertEqual(set(contracts), {".SPXW261022C100.5", ".SPXW261022P100.5"})
        self.assertEqual(contracts[".SPXW261022P100.5"], {
            "strike": 100.5, "side": "put", "multiplier": 100,
        })

    def test_unlisted_expiry_does_not_fall_back_to_another_expiry(self):
        with self.assertRaisesRegex(ValueError, "No listed"):
            self.fetch(self.payload(), expiration="261024")

    def test_wrong_root_or_side_in_contract_is_rejected(self):
        for symbol in (".SPX261022C100.5", ".SPXW261023C100.5", ".SPXW261022P100.5"):
            payload = self.payload()
            payload["data"]["items"][1]["expirations"][0]["strikes"][0]["call-streamer-symbol"] = symbol
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "inconsistent"):
                self.fetch(payload)

    def test_paginated_chain_is_not_treated_as_complete(self):
        payload = self.payload()
        payload["pagination"] = {"total-pages": 2}
        with self.assertRaisesRegex(ValueError, "paginated"):
            self.fetch(payload)


class WallSummaryTests(unittest.TestCase):
    def profile(self):
        return build_exposure_profile({
            f".SPY261022{side}{strike}": {
                "gamma": 0.1,
                "oi": 20 if (side, strike) in (("C", 110), ("P", 90)) else 1,
                "volume": 0,
            }
            for strike in (90, 100, 110) for side in "CP"
        }, 100)

    def test_directional_rule_above_below_between_and_on_walls(self):
        profile = self.profile()
        for spot, bias in ((111, "Bullish"), (89, "Bearish"), (100, "Neutral"), (90, "Neutral"), (110, "Neutral")):
            with self.subTest(spot=spot):
                self.assertEqual(get_wall_bias(profile, spot)["bias"], bias)

    def test_missing_observations_or_fallback_price_disable_direction(self):
        profile = self.profile()
        self.assertEqual(get_wall_bias(profile, 111, True)["bias"], "Unavailable")
        profile.loc[0, "put_oi_gex"] = math.nan
        self.assertEqual(get_wall_bias(profile, 111)["bias"], "Unavailable")

    def test_horizontal_bars_use_new_units_color_signs_and_bounded_rows(self):
        profile = self.profile()
        levels = get_wall_levels(profile, 100)
        fig = build_gex_bar_chart(
            profile, levels, "SPY", "261022", "25", {"strikes": [1000]},
        )
        self.assertEqual(fig.data[0].orientation, "h")
        self.assertEqual(list(fig.data[0].x), list(profile["net_oi_gex"] / 1e6))
        self.assertEqual(fig.data[0].marker.color[0], "#36cba5")
        self.assertEqual(fig.data[0].marker.color[-1], "#f45b76")
        self.assertEqual(tuple(fig.layout.yaxis.range), (2.5, -0.5))
        self.assertEqual(len(fig.layout.shapes), 3)
        self.assertEqual(fig.layout.shapes[0].line.color, "#36cba5")
        self.assertEqual(fig.layout.shapes[1].line.color, "#f45b76")
        self.assertIn("10/22/2026", fig.layout.title.text)
        with_pain = build_gex_bar_chart(
            profile, levels, "SPY", "261022", "25", {"strikes": [100]},
        )
        self.assertEqual(len(with_pain.layout.shapes), 4)
        self.assertEqual(with_pain.layout.shapes[-1].line.color, "#f5bf55")

    def test_full_chain_adds_only_missing_summary_subscriptions(self):
        ws = Mock()
        ws.recv.return_value = json.dumps({"type": "FEED_DATA", "data": [
            {"eventType": "Summary", "eventSymbol": ".SPY261022C200", "openInterest": 5},
        ]})
        with patch("simple_dashboard.time.time", side_effect=[0, 0, 100]):
            result = fetch_option_data(
                ws, [".SPY261022C100"], oi_symbols=[".SPY261022C100", ".SPY261022C200"],
            )
        subscriptions = json.loads(ws.send.call_args.args[0])["add"]
        self.assertEqual(len(subscriptions), 4)
        self.assertEqual(subscriptions[-1], {"symbol": ".SPY261022C200", "type": "Summary"})
        self.assertEqual(result[".SPY261022C200"]["oi"], 5)


class SnapshotSocket:
    def __init__(self, events):
        self.events = iter(events)
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def settimeout(self, value):
        pass

    def recv(self):
        return json.dumps({"type": "FEED_DATA", "data": next(self.events)})


class OHLCMarketSummaryTests(unittest.TestCase):
    def test_actual_trade_and_matching_summary_control(self):
        ws = SnapshotSocket([[
            dict(eventType="Trade", eventSymbol="SPX", price=105, dayId=20748, time=1234567),
            dict(eventType="Summary", eventSymbol="SPX", dayId=20748, dayOpenPrice=100,
                 dayHighPrice=110, dayLowPrice=95, dayClosePrice="NaN"),
        ]])
        result = get_underlying_snapshot(ws, "SPX")
        self.assertEqual([result[key] for key in ("open", "high", "low", "last", "price")], [100, 110, 95, 105, 105])
        self.assertTrue(result["comparable"])
        self.assertEqual(result["last_time"], 1234567)
        self.assertIn("Summary", [item["type"] for item in ws.sent[0]["add"]])

    def test_mismatched_sessions_disable_measured_direction(self):
        ws = SnapshotSocket([[
            dict(eventType="Trade", eventSymbol="SPX", price=105, dayId=20747),
            dict(eventType="Summary", eventSymbol="SPX", dayId=20748, dayOpenPrice=100, dayHighPrice=110, dayLowPrice=95),
        ]])
        result = get_underlying_snapshot(ws, "SPX")
        self.assertFalse(result["comparable"])
        context = get_open_to_wall_context(result["open"], result["last"], {}, result["comparable"])
        self.assertEqual(context["direction"], "Unavailable")

    def test_quote_midpoint_never_becomes_latest_close(self):
        ws = SnapshotSocket([[
            dict(eventType="Quote", eventSymbol="SPX", bidPrice=100, askPrice=102),
            dict(eventType="Trade", eventSymbol="OTHER", price=999),
            dict(eventType="Summary", eventSymbol="SPX", dayId=20748, dayOpenPrice="NaN", dayHighPrice="NaN", dayLowPrice="NaN"),
        ]])
        with patch("simple_dashboard.time.time", side_effect=[0, 0, 6]):
            result = get_underlying_snapshot(ws, "SPX")
        self.assertEqual(result["price"], 101)
        self.assertIsNone(result["last"])
        self.assertIsNone(result["open"])
        self.assertFalse(result["comparable"])

    def test_endpoint_controls_call_closer_and_put_closer(self):
        levels = {"call_wall": {"strike": 110}, "put_wall": {"strike": 90}}
        up = get_open_to_wall_context(100, 105, levels)
        self.assertEqual((up["direction"], up["change"], up["pct"]), ("Up", 5, 5))
        self.assertEqual(up["approach"], {"call": -5, "put": 5})
        self.assertEqual(up["nearest"], "Call wall")
        down = get_open_to_wall_context(100, 95, levels)
        self.assertEqual(down["approach"], {"call": 5, "put": -5})
        self.assertEqual(down["nearest"], "Put wall")

    def test_coincident_walls_both_closer_and_both_farther_are_not_forced_direction(self):
        same = {"call_wall": {"strike": 100}, "put_wall": {"strike": 100}}
        result = get_open_to_wall_context(90, 95, same)
        self.assertEqual(result["approach"], {"call": -5, "put": -5})
        self.assertEqual(result["nearest"], "Equidistant")
        result = get_open_to_wall_context(95, 90, same)
        self.assertEqual(result["approach"], {"call": 5, "put": 5})
        flat = get_open_to_wall_context(100, 100, same)
        self.assertEqual(flat["direction"], "Flat")
        self.assertEqual(flat["nearest"], "Equidistant")

    def test_missing_invalid_and_fallback_prices_have_no_direction(self):
        for opening, latest, comparable in ((None, 100, True), (0, 100, True), (100, float("nan"), True), (100, 105, False)):
            with self.subTest(opening=opening, latest=latest):
                result = get_open_to_wall_context(opening, latest, {}, comparable)
                self.assertEqual(result["direction"], "Unavailable")
                self.assertEqual(result["approach"], {})

    def test_closed_session_guard_prevents_subscriptions_and_stops_receive_loop(self):
        for function, arguments in (
            (get_underlying_snapshot, ("SPX",)), (fetch_option_data, ([".SPXW261022C6000"],)),
        ):
            with self.subTest(function=function.__name__):
                ws = Mock()
                with self.assertRaisesRegex(ValueError, "closed"):
                    function(ws, *arguments, session_guard=Mock(side_effect=ValueError("closed")))
                ws.send.assert_not_called()
                ws.recv.assert_not_called()
                with self.assertRaisesRegex(ValueError, "closed"):
                    function(ws, *arguments, session_guard=Mock(side_effect=[None, ValueError("closed")]))
                ws.send.assert_called_once()
                ws.recv.assert_not_called()


if __name__ == "__main__":
    unittest.main()
