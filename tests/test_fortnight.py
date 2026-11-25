"""The schedule on fourteen real days: it must act where it pays and never lose."""

import random
import unittest

import fixture_fortnight as days
from _loader import load

opt = load("optimizer")
const = load("const")


def battery(efficiency, standby=0.0):
    """The author's battery: about 6.3 kWh, 3.4 kW charging, 3.6 kW discharging."""
    return opt.Battery(
        capacity=6.3, floor=15, charge_eff=efficiency, grid_eff=efficiency * 0.95,
        grid_power=3.54, shift_cost=const.DEFAULT_SHIFT_COST / 100, release_slots=2,  # 1.5 h
        standby=standby,
        discharge_power=3.6,
    )


def run(plan_battery, real_efficiency, caution, seed):
    """Plan every day with the prices of today and tomorrow and a noisy forecast,
    carry today's modes out on the real day. Returns EUR saved and days with a grid charge."""
    rng = random.Random(seed)
    soc, saved, charged_days = 15.0, 0.0, 0
    total = len(days.GRID)
    for d in range(total):
        hours = range(d * 24, min((d + 2) * 24, total * 24))
        level = {n: rng.uniform(0.8, 1.25) for n in (d, d + 1)}
        forecast = [
            opt.Slot(h * 3600.0, 1.0, days.PRICES[h // 24][h % 24],
                     days.GRID[h // 24][h % 24] * level[h // 24] * rng.uniform(0.7, 1.3), 0.0)
            for h in hours
        ]
        real = [
            opt.Slot(h * 3600.0, 1.0, days.PRICES[h // 24][h % 24], days.GRID[h // 24][h % 24], 0.0)
            for h in hours
        ][:24]
        plan = opt.cautious_plan(forecast, soc, plan_battery, efficiency=caution)
        done = opt.replay(real, soc, battery(real_efficiency), opt.Plan(plan.slots[:24], 0, 0, 0))
        saved += sum(s.load * s.price for s in real) - done.cost
        charged_days += any(s.mode == const.MODE_GRID_CHARGE for s in done.slots)
        soc = done.slots[-1].soc
    cheapest = min(min(p) for p in days.PRICES)
    return saved + (soc - 15) / 100 * 6.3 * cheapest, charged_days


class FortnightTest(unittest.TestCase):
    def test_measured_efficiency_pays_on_every_run(self):
        caution = const.CAUTION_EFFICIENCY_MEASURED
        results = [run(battery(0.735), 0.73, caution, seed) for seed in range(10)]
        saved = [r[0] for r in results]
        self.assertGreater(min(saved), 0.5)  # EUR in 14 days, with forecast errors
        self.assertGreater(sum(saved) / len(saved), 1.0)
        self.assertGreaterEqual(min(r[1] for r in results), 3)

    def test_worse_battery_than_planned_still_pays(self):
        results = [run(battery(0.78), 0.70, const.CAUTION_EFFICIENCY, seed) for seed in range(10)]
        self.assertGreater(min(r[0] for r in results), 0.0)

    def test_standby_of_version_1_3_blocked_every_plan(self):
        results = [run(battery(0.78, standby=0.05), 0.78, const.CAUTION_EFFICIENCY, s) for s in range(3)]
        self.assertEqual([r[1] for r in results], [0, 0, 0])


if __name__ == "__main__":
    unittest.main()
