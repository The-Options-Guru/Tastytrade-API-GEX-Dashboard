import math
import unittest

from utils.exposure_profile import build_exposure_profile, get_wall_levels
from utils.gex_calculator import GEXCalculator, parse_option_symbol


class ExposureProfileTests(unittest.TestCase):
    def test_formula_and_volume_are_distinct_from_oi(self):
        profile = build_exposure_profile({
            ".SPY261022C100": {"gamma": 0.1, "oi": 10, "volume": 30},
            ".SPY261022P100": {"gamma": 0.2, "oi": 20, "volume": 5},
        }, 100)
        row = profile.iloc[0]
        self.assertEqual(row["call_oi_gex"], 10000)
        self.assertEqual(row["put_oi_gex"], 40000)
        self.assertEqual(row["net_oi_gex"], -30000)
        self.assertEqual(row["net_volume_gex"], 20000)
        self.assertEqual(row["call_volume"], 30)
        self.assertEqual(row["put_volume"], 5)

    def test_walls_use_gamma_oi_not_oi_volume_or_spot_side(self):
        profile = build_exposure_profile({
            ".SPY261022C90": {"gamma": 0.5, "oi": 50, "volume": 1},
            ".SPY261022C110": {"gamma": 0.01, "oi": 100, "volume": 9000},
            ".SPY261022P90": {"gamma": 0.01, "oi": 100, "volume": 9000},
            ".SPY261022P110": {"gamma": 0.5, "oi": 50, "volume": 1},
        }, 100)
        walls = get_wall_levels(profile, 100)
        self.assertEqual(walls["call_wall"]["strike"], 90)
        self.assertEqual(walls["put_wall"]["strike"], 110)
        self.assertEqual(walls["atm_strike"], 90)

    def test_ties_are_deterministic_and_walls_can_coincide(self):
        data = {
            f".SPY261022{side}{strike}": {"gamma": 0.1, "oi": 10, "volume": 0}
            for strike in (110, 100, 90) for side in "CP"
        }
        profile = build_exposure_profile(data, 100)
        self.assertEqual(profile["strike"].tolist(), [110, 100, 90])
        walls = get_wall_levels(profile, 100)
        self.assertEqual(walls["call_wall"]["strike"], 100)
        self.assertEqual(walls["put_wall"]["strike"], 100)
        self.assertEqual(get_wall_levels(profile, 105)["call_wall"]["strike"], 100)

    def test_missing_invalid_and_nonfinite_inputs_remain_unknown(self):
        for bad in (None, "NaN", float("inf"), "-1", "bad", True):
            with self.subTest(bad=bad):
                profile = build_exposure_profile({
                    ".SPY261022C100": {"gamma": bad, "oi": 10, "volume": 5},
                }, 100)
                row = profile.iloc[0]
                self.assertTrue(math.isnan(row["call_oi_gex"]))
                self.assertTrue(math.isnan(row["net_oi_gex"]))
                self.assertEqual(row["call_volume"], 5)
                self.assertIsNone(get_wall_levels(profile, 100)["call_wall"])
        profile = build_exposure_profile({
            ".SPY261022C100": {"gamma": 0.1, "oi": None},
            ".SPY261022P100": {"gamma": 0.1, "oi": 1, "volume": 0},
        }, 100)
        self.assertTrue(math.isnan(profile.iloc[0]["net_volume_gex"]))
        self.assertTrue(math.isnan(profile.iloc[0]["net_oi_gex"]))

    def test_zero_is_valid_but_not_a_wall(self):
        profile = build_exposure_profile({
            f".SPY261022{side}100": {"gamma": 0.1, "oi": 0, "volume": 0}
            for side in "CP"
        }, 100)
        self.assertEqual(profile.iloc[0]["net_oi_gex"], 0)
        self.assertEqual(profile.iloc[0]["net_volume_gex"], 0)
        walls = get_wall_levels(profile, 100)
        self.assertIsNone(walls["call_wall"])
        self.assertIsNone(walls["put_wall"])

    def test_partial_same_side_aggregation_is_not_reported_as_complete(self):
        profile = build_exposure_profile({
            ".SPY261022C100": {"gamma": 0.1, "oi": 10, "volume": 5},
            ".SPY261022C100.0": {"gamma": 0.1, "oi": None, "volume": 7},
        }, 100)
        self.assertTrue(math.isnan(profile.iloc[0]["call_oi_gex"]))
        self.assertEqual(profile.iloc[0]["call_volume"], 12)

    def test_empty_input_and_invalid_spot(self):
        profile = build_exposure_profile({"SPY": {"volume": 1}}, 100)
        self.assertTrue(profile.empty)
        self.assertEqual(get_wall_levels(profile, 100), {
            "call_wall": None, "put_wall": None, "atm_strike": None,
        })
        for spot in (0, -1, None, float("nan"), float("inf")):
            with self.subTest(spot=spot), self.assertRaises(ValueError):
                build_exposure_profile({}, spot)

    def test_decimal_strikes_are_not_truncated_and_legacy_units_are_unchanged(self):
        symbol = ".SPY261022C100.5"
        self.assertEqual(parse_option_symbol(symbol)["strike"], 100.5)
        self.assertIsNone(parse_option_symbol(symbol + "bad"))
        calc = GEXCalculator(spot_price=200)
        calc.update_gamma(symbol, 0.1, 10)
        self.assertEqual(calc.get_gex_by_strike().iloc[0]["call_gex"], 20000)
        profile = build_exposure_profile({symbol: {"gamma": 0.1, "oi": 10}}, 200)
        self.assertEqual(profile.iloc[0]["call_oi_gex"], 40000)
        self.assertEqual(profile.iloc[0]["strike"], 100.5)


if __name__ == "__main__":
    unittest.main()
