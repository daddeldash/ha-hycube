"""Schedule optimisation."""

import time
import unittest

import fixture_day
from _loader import load

opt = load("optimizer")
const = load("const")

STANDARD, HOLD, GRID_CHARGE = const.MODE_STANDARD, const.MODE_HOLD, const.MODE_GRID_CHARGE


def battery(**kw):
    values = dict(
        capacity=6.6, floor=15, charge_eff=0.78, grid_eff=0.74, grid_power=2.5,
        shift_cost=0.01,
    )
    values.update(kw)
    return opt.Battery(**values)


def slots(prices, load=0.25, pv=0.0):
    """Quarter-hour slots; load and pv are kWh per slot or a list of them."""
    out = []
    for i, price in enumerate(prices):
        out.append(
            opt.Slot(
                start=i * 900.0,
                hours=0.25,
                price=price,
                load=load[i] if isinstance(load, list) else load,
                pv=pv[i] if isinstance(pv, list) else pv,
            )
        )
    return out


def changes(plan):
    return sum(a.mode != b.mode for a, b in zip(plan.slots, plan.slots[1:]))


class OptimizeTest(unittest.TestCase):
    def test_flat_prices_stay_standard(self):
        plan = opt.optimize(slots([0.30] * 48), 60, battery())
        self.assertEqual({s.mode for s in plan.slots}, {STANDARD})
        self.assertEqual(plan.savings, 0.0)
        self.assertEqual(plan.cost, plan.baseline_cost)

    def test_grid_charge_before_expensive_hours(self):
        prices = [0.20] * 16 + [0.40] * 16
        plan = opt.optimize(slots(prices), 15, battery())
        charging = [s for s in plan.slots if s.mode == GRID_CHARGE]
        self.assertTrue(charging)
        self.assertTrue(all(s.price == 0.20 for s in charging))
        # 4 kWh are needed in the expensive hours, 0.74 of the grid energy arrives.
        taken = sum(s.battery for s in charging)
        self.assertAlmostEqual(taken * 0.74, 4.0, delta=0.5)
        expensive = plan.slots[16:]
        self.assertLess(sum(s.grid for s in expensive), 0.6)
        self.assertEqual(expensive[0].source, const.SOURCE_BATTERY)
        # Each of the 4 kWh costs 0.20 / 0.74 instead of 0.40.
        self.assertAlmostEqual(plan.savings, 4.0 * (0.40 - 0.20 / 0.74), delta=0.1)
        self.assertEqual(plan.next_grid_charge().start, charging[0].start)

    def test_charges_last_when_hold_is_not_available(self):
        prices = [0.20] * 16 + [0.40] * 16
        plan = opt.optimize(slots(prices), 15, battery(allow_hold=False))
        modes = [s.mode for s in plan.slots[:16]]
        first = modes.index(GRID_CHARGE)
        # Charged energy would drain into cheap hours, so charging ends right
        # before the price step.
        self.assertEqual(set(modes[first:]), {GRID_CHARGE})
        self.assertNotIn(HOLD, {s.mode for s in plan.slots})

    def test_no_grid_charge_when_the_spread_is_too_small(self):
        prices = [0.30] * 16 + [0.36] * 16  # 0.30 / 0.74 > 0.36
        plan = opt.optimize(slots(prices), 15, battery())
        self.assertEqual({s.mode for s in plan.slots}, {STANDARD})

    def test_grid_charge_can_be_switched_off(self):
        prices = [0.20] * 16 + [0.40] * 16
        plan = opt.optimize(slots(prices), 15, battery(allow_grid_charge=False))
        self.assertNotIn(GRID_CHARGE, {s.mode for s in plan.slots})

    def test_hold_saves_stored_energy_for_the_peak(self):
        prices = [0.30] * 8 + [0.45] * 8
        usable = (60 - 15) / 100 * 6.6
        plan = opt.optimize(
            slots(prices, load=0.5), 60, battery(allow_grid_charge=False)
        )
        self.assertEqual({s.mode for s in plan.slots[:8]}, {HOLD})
        self.assertEqual({s.source for s in plan.slots[:8]}, {const.SOURCE_GRID})
        self.assertAlmostEqual(-sum(s.battery for s in plan.slots[8:]), usable, places=3)
        self.assertAlmostEqual(plan.savings, usable * 0.15, places=3)

    def test_hold_ends_early_when_the_battery_needs_time_to_resume(self):
        # After "hold" the battery stays idle for 6 slots: the plan has to
        # leave "hold" 6 slots before the peak.
        prices = [0.30] * 12 + [0.45] * 8
        usable = (60 - 15) / 100 * 6.6
        plan = opt.optimize(
            slots(prices, load=0.5),
            60,
            battery(allow_grid_charge=False, release_slots=6),
        )
        self.assertEqual([s.mode for s in plan.slots[:12]], [HOLD] * 6 + [STANDARD] * 6)
        self.assertEqual({s.source for s in plan.slots[:12]}, {const.SOURCE_GRID})
        self.assertEqual(sum(s.battery for s in plan.slots[:12]), 0.0)
        self.assertAlmostEqual(-sum(s.battery for s in plan.slots[12:]), usable, places=3)

    def test_short_hold_is_not_worth_the_idle_time(self):
        # The peak starts in 4 slots, the battery would be back after 6.
        prices = [0.30] * 4 + [0.45] * 4 + [0.30] * 8
        plan = opt.optimize(
            slots(prices, load=0.5),
            60,
            battery(allow_grid_charge=False, release_slots=6),
        )
        self.assertEqual({s.mode for s in plan.slots}, {STANDARD})

    def test_battery_idles_after_hold_was_left(self):
        plan = opt.optimize(
            slots([0.30] * 8, load=0.5), 60, battery(release_slots=6), STANDARD, idle_slots=3
        )
        self.assertEqual([s.battery for s in plan.slots[:3]], [0.0] * 3)
        self.assertEqual([s.source for s in plan.slots[:4]], [const.SOURCE_GRID] * 3 + [const.SOURCE_BATTERY])

    def test_device_in_hold_while_hold_is_not_planned(self):
        plan = opt.optimize(
            slots([0.30] * 8 + [0.60] * 8, load=0.5),
            60,
            battery(allow_hold=False, release_slots=2),
            HOLD,
        )
        self.assertNotIn(HOLD, {s.mode for s in plan.slots})
        # Leaving "hold" still costs the idle time: no discharge in two slots.
        self.assertTrue(all(s.battery >= 0 for s in plan.slots[:2]))
        self.assertLess(plan.slots[8].battery, 0)

    def test_without_hold_the_battery_runs_as_usual(self):
        prices = [0.30] * 8 + [0.45] * 8
        plan = opt.optimize(
            slots(prices, load=0.5),
            60,
            battery(allow_hold=False, allow_grid_charge=False),
        )
        self.assertEqual({s.mode for s in plan.slots}, {STANDARD})
        self.assertEqual(plan.slots[0].source, const.SOURCE_BATTERY)

    def test_small_price_ripple_does_not_toggle_the_mode(self):
        # Quarter-hour prices zigzag within each hour, as Tibber's do.
        prices = [0.380, 0.364, 0.360, 0.354] * 8
        plan = opt.optimize(slots(prices, load=0.3), 40, battery())
        self.assertLessEqual(changes(plan), 2)

    def test_solar_surplus_charges_and_is_classified(self):
        pv = [0.0] * 4 + [1.0] * 8 + [0.0] * 4
        plan = opt.optimize(slots([0.30] * 16, load=0.25, pv=pv), 15, battery())
        self.assertEqual({s.source for s in plan.slots[4:12]}, {const.SOURCE_SOLAR})
        charged = sum(s.battery for s in plan.slots[4:12])
        self.assertAlmostEqual(charged, 8 * 0.75, places=6)
        self.assertAlmostEqual(plan.slots[11].soc, 15 + charged * 0.78 / 6.6 * 100, places=6)
        self.assertEqual(plan.slots[12].source, const.SOURCE_BATTERY)

    def test_full_battery_exports(self):
        plan = opt.optimize(
            slots([0.30] * 4, load=0.0, pv=1.0), 100, battery(export_price=0.08)
        )
        self.assertAlmostEqual(sum(s.export for s in plan.slots), 4.0)
        self.assertAlmostEqual(plan.cost, -0.32)

    def test_soc_stays_within_limits_and_savings_are_never_negative(self):
        day = fixture_day
        for soc in (5, 15, 18, 40, 75, 100):
            for scale in (0.5, 1.0, 3.0):
                prices = [min(day.PRICES) + (p - min(day.PRICES)) * scale for p in day.PRICES]
                load = [day.LOAD[i // 4] / 4 for i in range(96)]
                pv = [day.PV[i // 4] / 4000 * 6 for i in range(96)]
                plan = opt.optimize(
                    slots(prices, load, pv), soc, battery(max_grid_soc=90, release_slots=6)
                )
                self.assertGreaterEqual(plan.savings, 0.0)
                for s in plan.slots:
                    self.assertGreaterEqual(s.soc, 15 - 1e-6)
                    self.assertLessEqual(s.soc, 100 + 1e-6)
                    self.assertGreaterEqual(s.grid, -1e-9)
                    if s.mode == GRID_CHARGE:
                        self.assertLessEqual(s.soc, 90 + 1e-6)
                    # Energy balance of the slot.
                    self.assertAlmostEqual(
                        s.pv + s.grid, s.load + s.export + s.battery, places=6
                    )

    def test_real_day_is_fast_and_consistent(self):
        day = fixture_day
        two_days = slots(
            day.PRICES * 2,
            [day.LOAD[i // 4 % 24] / 4 for i in range(192)],
            [day.PV[i // 4 % 24] / 4000 for i in range(192)],
        )
        started = time.perf_counter()
        plan = opt.optimize(two_days, 18, battery(release_slots=6))
        self.assertLess(time.perf_counter() - started, 5)
        self.assertEqual(len(plan.slots), 192)
        self.assertAlmostEqual(
            plan.cost, sum(s.grid * s.price for s in plan.slots), places=6
        )
        # Cheapest price 0.288, dearest 0.386: after losses that is no gain.
        self.assertNotIn(GRID_CHARGE, {s.mode for s in plan.slots})

    def test_blocks_merge_equal_slots(self):
        prices = [0.20] * 16 + [0.40] * 16
        plan = opt.optimize(slots(prices), 15, battery())
        blocks = plan.blocks()
        self.assertEqual(blocks[0].start, 0.0)
        self.assertEqual(blocks[-1].end, 32 * 900.0)
        for a, b in zip(blocks, blocks[1:]):
            self.assertEqual(a.end, b.start)
            self.assertNotEqual((a.mode, a.source), (b.mode, b.source))
        self.assertAlmostEqual(sum(b.grid for b in blocks), sum(s.grid for s in plan.slots))
        self.assertEqual(blocks[-1].soc, plan.slots[-1].soc)

    def test_empty_horizon(self):
        plan = opt.optimize([], 50, battery())
        self.assertEqual(plan.slots, [])
        self.assertIsNone(plan.next_grid_charge())


if __name__ == "__main__":
    unittest.main()
