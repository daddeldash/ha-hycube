"""Correction of the PV forecast."""

import unittest
from datetime import date, timedelta

from _loader import load

solar = load("solar")

TODAY = date(2026, 6, 15)
# kWh per hour on a clear day, 6 to 20 o'clock.
CLEAR = [0.0] * 6 + [0.2, 0.6, 1.0, 1.4, 1.7, 1.9, 2.0, 1.9, 1.7, 1.4, 1.0, 0.6, 0.2] + [0.0] * 5


def pairs(count, actual):
    """`count` days before TODAY; actual(hour, forecast) -> produced kWh."""
    return [
        (TODAY - timedelta(days=i), [actual(h, f) for h, f in enumerate(CLEAR)], CLEAR)
        for i in range(1, count + 1)
    ]


class PvCorrectionTest(unittest.TestCase):
    def test_overall_factor(self):
        fit = solar.fit_correction(pairs(20, lambda h, f: f * 0.8), TODAY)
        self.assertAlmostEqual(fit.overall, 0.8, places=3)
        self.assertEqual(fit.days, 20)
        self.assertAlmostEqual(fit.factor(12), 0.8, places=2)
        # Night hours carry no energy: they take the overall factor.
        self.assertAlmostEqual(fit.factor(2), 0.8, places=3)

    def test_shaded_morning(self):
        fit = solar.fit_correction(pairs(30, lambda h, f: f * (0.4 if h < 10 else 1.0)), TODAY)
        self.assertLess(fit.factor(8), 0.6)
        self.assertLess(fit.factor(9), 0.6)
        self.assertAlmostEqual(fit.factor(13), 1.0, delta=0.05)
        self.assertAlmostEqual(fit.factor(17), 1.0, delta=0.05)

    def test_too_few_days(self):
        self.assertIsNone(solar.fit_correction(pairs(4, lambda h, f: f), TODAY))
        # Days with next to no forecast do not count.
        dull = [(d, a, [v / 100 for v in f]) for d, a, f in pairs(20, lambda h, f: f)]
        self.assertIsNone(solar.fit_correction(dull, TODAY))

    def test_limits(self):
        self.assertEqual(solar.fit_correction(pairs(20, lambda h, f: f * 3), TODAY).overall, 1.5)
        low = solar.fit_correction(pairs(20, lambda h, f: 0.0), TODAY)
        self.assertEqual(low.overall, 0.3)
        self.assertEqual(low.factor(12), 0.3)

    def test_recent_days_count_more(self):
        old = [(d - timedelta(days=90), a, f) for d, a, f in pairs(20, lambda h, f: f * 0.5)]
        fit = solar.fit_correction(old + pairs(20, lambda h, f: f), TODAY)
        self.assertGreater(fit.overall, 0.9)

    def test_missing_hours_and_today_are_skipped(self):
        data = pairs(10, lambda h, f: None if h == 12 else f)
        data.append((TODAY, [9.0] * 24, CLEAR))
        fit = solar.fit_correction(data, TODAY)
        self.assertEqual(fit.days, 10)
        self.assertAlmostEqual(fit.overall, 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
