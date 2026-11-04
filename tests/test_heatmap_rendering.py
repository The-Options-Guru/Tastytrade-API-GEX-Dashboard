import unittest

from simple_dashboard import build_strike_heatmap, format_heatmap_value, generate_option_symbols
from utils.exposure_profile import build_exposure_profile, get_wall_levels


class HeatmapRenderingTests(unittest.TestCase):
    def make_profile(self, center):
        symbols = generate_option_symbols(center, "SPXW", "261022", 100, 100, 5)
        data = {
            symbol: {"gamma": 0.01, "oi": index + 1, "volume": index * 10}
            for index, symbol in enumerate(symbols)
        }
        return build_exposure_profile(data, center)

    def test_50_above_and_50_below_request_202_contracts_including_center(self):
        symbols = generate_option_symbols(7665, "SPXW", "261022", 50, 50, 5)
        self.assertEqual(len(symbols), 202)
        self.assertIn(".SPXW261022C7415", symbols)
        self.assertIn(".SPXW261022P7915", symbols)
        self.assertIn(".SPXW261022C7665", symbols)
        self.assertIn(".SPXW261022P7665", symbols)

    def test_large_strike_values_do_not_expand_the_axis_beyond_rows(self):
        for center in (100, 7665, 20000):
            with self.subTest(center=center):
                profile = self.make_profile(center)
                levels = get_wall_levels(profile, center)
                fig = build_strike_heatmap(profile, levels, "SPX", "261022", "Gamma notional", "25")
                self.assertEqual(tuple(fig.layout.yaxis.range), (24.5, -0.5))
                self.assertEqual(list(fig.data[0].y), list(range(25)))
                self.assertEqual(len(fig.data[1].text), 75)
                for shape in fig.layout.shapes:
                    self.assertGreaterEqual(shape.y0, -0.5)
                    self.assertLessEqual(shape.y1, 24.5)
                self.assertEqual(fig.layout.paper_bgcolor, "#101217")
                self.assertIn("10/22/2026", fig.layout.title.text)

    def test_all_strikes_and_same_strike_wall_lines_stay_aligned(self):
        profile = self.make_profile(7665)
        levels = get_wall_levels(profile, 7665)
        levels["call_wall"] = {"strike": 7665, "gex": 1}
        levels["put_wall"] = {"strike": 7665, "gex": 1}
        fig = build_strike_heatmap(profile, levels, "SPX", "261022", "Gamma notional", "All")
        self.assertEqual(len(fig.data[0].y), 201)
        lines = [shape for shape in fig.layout.shapes if shape.type == "line"]
        self.assertEqual(len(lines), 3)
        self.assertEqual([line.y0 for line in lines], [99.65, 100.35, 100])
        self.assertTrue(all(line.y0 == line.y1 for line in lines))
        self.assertEqual([line.line.color for line in lines], ["#36cba5", "#f45b76", "#36a9ff"])
        self.assertEqual(fig.layout.yaxis.ticktext[100], "7,665")

    def test_unknown_direction_and_missing_values_are_not_colored_zero(self):
        profile = build_exposure_profile({
            ".SPXW261022C7665": {"gamma": 0.01, "oi": 10, "volume": 0},
            ".SPXW261022P7665": {"gamma": 0.01, "oi": 20},
        }, 7665)
        levels = get_wall_levels(profile, 7665)
        fig = build_strike_heatmap(profile, levels, "SPX", "261022", "Gamma notional", "25")
        self.assertIsNone(fig.data[0].z[0][0])
        self.assertEqual(list(fig.data[1].text)[:2], ["N/A", "N/A"])
        self.assertEqual(fig.data[1].text[2], format_heatmap_value(profile.iloc[0]["net_oi_gex"]))
        raw = build_strike_heatmap(profile, levels, "SPX", "261022", "Contract volume", "25")
        self.assertEqual(list(raw.data[1].text), ["N/A", "0", "N/A"])
        self.assertEqual(raw.data[0].zmin, 0)
        self.assertEqual(format_heatmap_value(-1234000), "-1.23M")
        self.assertEqual(format_heatmap_value(-0.001), "0")


if __name__ == "__main__":
    unittest.main()
