"""Capacity and efficiency estimation."""

import unittest

from _loader import load

energy = load("energy")


def feed(est, points):
    """points: (soc, measured Wh, opposite Wh); returns how many samples were taken."""
    return sum(est.update(*p) for p in points)


class CapacityEstimatorTest(unittest.TestCase):
    def test_discharge_capacity(self):
        est = energy.CapacityEstimator()
        # 66 Wh per percent while falling from 80 to 50 %.
        points = [(80 - i, i * 66.0, 0.0) for i in range(31)]
        self.assertEqual(feed(est, points), 1)
        self.assertAlmostEqual(est.kwh, 6.6, places=2)

    def test_charge_capacity_ignores_trickle_before_the_stretch(self):
        est = energy.CapacityEstimator(rising=True)
        points = [(13, 0.0, 0.0), (12, 0.0, 5.0)]
        # Hours of standby trickle at 12 % that never raise the SoC.
        points += [(12, wh, 5.0) for wh in (100.0, 300.0, 500.0)]
        # Then 85 Wh per percent from 12 to 28 %: one sample after 10 % rise.
        points += [(12 + i, 500.0 + i * 85.0, 5.0) for i in range(1, 17)]
        feed(est, points)
        self.assertEqual(est.samples, 1)
        self.assertAlmostEqual(est.kwh, 8.5, places=2)

    def test_opposite_flow_discards_the_stretch(self):
        est = energy.CapacityEstimator(rising=True)
        points = [(20 + i, i * 85.0, 0.0) for i in range(8)]
        # 200 Wh discharged in between, then charging continues.
        points += [(28 + i, 680.0 + i * 85.0, 200.0) for i in range(8)]
        self.assertEqual(feed(est, points), 0)

    def test_step_in_the_wrong_direction_discards_the_stretch(self):
        est = energy.CapacityEstimator()
        points = [(80 - i, i * 66.0, 0.0) for i in range(15)]
        points += [(67, 14 * 66.0, 0.0)]
        points += [(66 - i, (15 + i) * 66.0, 0.0) for i in range(15)]
        self.assertEqual(feed(est, points), 0)

    def test_running_mean_and_persistence(self):
        est = energy.CapacityEstimator({"kwh": 6.0, "samples": 4, "last_sample": 6.0})
        feed(est, [(80 - i, i * 70.0, 0.0) for i in range(22)])
        self.assertEqual(est.samples, 5)
        self.assertAlmostEqual(est.kwh, 6.0 * 0.7 + 7.0 * 0.3, places=2)
        self.assertEqual(energy.CapacityEstimator(est.as_dict()).kwh, est.kwh)


class EfficiencyTest(unittest.TestCase):
    def test_needs_enough_samples_on_both_sides(self):
        out = energy.CapacityEstimator({"kwh": 6.6, "samples": 5})
        into = energy.CapacityEstimator({"kwh": 8.5, "samples": 2}, rising=True)
        self.assertIsNone(energy.round_trip_efficiency(out, into, 3))
        into.samples = 3
        self.assertAlmostEqual(energy.round_trip_efficiency(out, into, 3), 0.776, places=3)

    def test_is_clamped(self):
        out = energy.CapacityEstimator({"kwh": 9.0, "samples": 5})
        into = energy.CapacityEstimator({"kwh": 8.0, "samples": 5}, rising=True)
        self.assertEqual(energy.round_trip_efficiency(out, into, 3), 1.0)


if __name__ == "__main__":
    unittest.main()
