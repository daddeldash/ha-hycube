"""Round-trip efficiency from the charge and discharge counters."""

import unittest
from datetime import date, timedelta

from _loader import load

energy = load("energy")

START = date(2026, 10, 1)


def days(count, charge=3.0, discharge=2.25, soc=15.0):
    """`count` days with the SoC known at every start."""
    return [(START + timedelta(days=i), soc, charge, discharge) for i in range(count + 1)]


class CounterEfficiencyTest(unittest.TestCase):
    def test_ratio_over_the_window(self):
        result = energy.counter_efficiency(days(30), 6.3, 15.0, 14)
        self.assertAlmostEqual(result.efficiency, 0.75)
        self.assertEqual((result.start, result.end), (START, START + timedelta(days=30)))
        self.assertAlmostEqual(result.charged, 90.0)

    def test_energy_left_in_the_battery_counts(self):
        rows = days(30)
        last = rows[-1]
        rows[-1] = (last[0], 65.0, last[2], last[3])  # 50 % = 3.15 kWh still inside
        result = energy.counter_efficiency(rows, 6.3, 15.0, 14)
        self.assertAlmostEqual(result.efficiency, (67.5 + 3.15) / 90, places=3)

    def test_a_gap_ends_the_window(self):
        rows = days(40)
        del rows[10]  # no statistics for that day
        result = energy.counter_efficiency(rows, 6.3, 15.0, 14)
        self.assertEqual(result.start, START + timedelta(days=11))

    def test_unknown_soc_in_between_is_fine(self):
        rows = [(d, None if 0 < i < 30 else s, c, x) for i, (d, s, c, x) in enumerate(days(30))]
        self.assertAlmostEqual(energy.counter_efficiency(rows, 6.3, 15.0, 14).efficiency, 0.75)

    def test_too_little_data(self):
        self.assertIsNone(energy.counter_efficiency(days(10), 6.3, 15.0, 14))
        self.assertIsNone(energy.counter_efficiency(days(30, charge=0.3, discharge=0.2), 6.3, 15.0, 14))
        self.assertIsNone(energy.counter_efficiency([], 6.3, 15.0, 14))

    def test_clamped(self):
        self.assertEqual(energy.counter_efficiency(days(30, discharge=9.0), 6.3, 15.0, 14).efficiency, 1.0)


if __name__ == "__main__":
    unittest.main()
