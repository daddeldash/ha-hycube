"""Safeguards that keep the schedule from costing more than it saves."""

import random
import unittest
from datetime import date, timedelta

import fixture_day
import fixture_spread
from _loader import load

opt = load("optimizer")
const = load("const")
audit = load("audit")

STANDARD, HOLD, GRID_CHARGE = const.MODE_STANDARD, const.MODE_HOLD, const.MODE_GRID_CHARGE


def battery(**kw):
    """The author's battery as the plan sees it."""
    values = dict(
        capacity=6.62, floor=15, charge_eff=0.78, grid_eff=0.78 * 0.95,
        grid_power=3.36 / 0.95, shift_cost=0.01, release_slots=6,
        standby=0.05, discharge_power=3.6,
    )
    values.update(kw)
    return opt.Battery(**values)


def day_slots(prices, load, pv, load_factor=1.0, pv_factor=1.0):
    """96 quarter-hour slots from prices per slot, kWh and Wh per hour."""
    return [
        opt.Slot(
            i * 900.0, 0.25, prices[i],
            load[i // 4] / 4 * load_factor, pv[i // 4] / 4000 * pv_factor,
        )
        for i in range(96)
    ]


def plan_for(prices, load, pv, soc=15, bat=None):
    return opt.cautious_plan(day_slots(prices, load, pv), soc, bat or battery())


def stretched(prices, factor):
    low = min(prices)
    return [low + (p - low) * factor for p in prices]


class StandbyTest(unittest.TestCase):
    def test_standby_is_paid_while_the_battery_holds_energy(self):
        slots = [opt.Slot(i * 900.0, 0.25, 0.40, 0.0, 0.0) for i in range(8)]
        charged = opt.optimize(slots, 60, battery())
        self.assertAlmostEqual(charged.cost, 8 * 0.25 * 0.05 * 0.40)
        empty = opt.optimize(slots, 15, battery())
        self.assertEqual(empty.cost, 0.0)  # asleep

    def test_a_long_wait_eats_the_gain(self):
        # Cheap for 2 h, then 14 h at a middling price, dear at the end.
        prices = [0.25] * 8 + [0.33] * 56 + [0.42] * 16
        slots = [opt.Slot(i * 900.0, 0.25, p, 0.2, 0.0) for i, p in enumerate(prices)]
        without = opt.optimize(slots, 15, battery(standby=0.0))
        self.assertIn(GRID_CHARGE, {s.mode for s in without.slots})
        with_standby = opt.optimize(slots, 15, battery())
        self.assertEqual({s.mode for s in with_standby.slots}, {STANDARD})

    def test_a_short_wait_still_pays(self):
        prices = [0.25] * 8 + [0.45] * 16
        slots = [opt.Slot(i * 900.0, 0.25, p, 0.3, 0.0) for i, p in enumerate(prices)]
        plan = opt.optimize(slots, 15, battery())
        self.assertIn(GRID_CHARGE, {s.mode for s in plan.slots})
        self.assertGreater(plan.savings, 0.2)

    def test_discharge_power_limits_what_the_battery_covers(self):
        slots = [opt.Slot(0.0, 0.25, 0.40, 2.0, 0.0)]  # 8 kW for a quarter of an hour
        plan = opt.optimize(slots, 80, battery(standby=0.0))
        self.assertAlmostEqual(-plan.slots[0].battery, 3.6 * 0.25)
        self.assertAlmostEqual(plan.slots[0].grid, 2.0 - 0.9)


class CautiousPlanTest(unittest.TestCase):
    def test_announced_grid_charge_of_16_november_is_dropped(self):
        day = fixture_spread
        # What was installed that day: no standby, 80 %, 3.5 kW, charge up to 75 %.
        old = opt.Battery(
            capacity=6.62, floor=15, charge_eff=0.80, grid_eff=0.76, grid_power=3.5,
            shift_cost=0.01, release_slots=6, max_grid_soc=75,
        )
        announced = opt.optimize(day_slots(day.PRICES, day.LOAD, day.PV), 15, old)
        block = announced.next_grid_charge()
        self.assertEqual((block.start, block.end), (3.5 * 3600, 5 * 3600))
        self.assertAlmostEqual(block.soc, 75.0, places=1)
        self.assertAlmostEqual(announced.savings, 0.27, places=2)
        # With what the storage system draws itself, nothing is left of it.
        for cap in (75, 95, 100):
            plan = plan_for(day.PRICES, day.LOAD, day.PV, bat=battery(max_grid_soc=cap))
            self.assertEqual({s.mode for s in plan.slots}, {STANDARD}, cap)
            self.assertEqual(plan.savings, 0.0)

    def test_a_clear_gain_is_still_taken(self):
        day = fixture_spread
        plan = plan_for(stretched(day.PRICES, 2.5), day.LOAD, day.PV)
        self.assertIn(GRID_CHARGE, {s.mode for s in plan.slots})
        self.assertGreater(plan.savings, 0.1)
        # The plan rests on the worst case; the forecast promises at least that.
        self.assertGreaterEqual(plan.expected, plan.savings)

    def test_charge_is_sized_for_the_cautious_need(self):
        day = fixture_spread
        prices = stretched(day.PRICES, 3.0)
        load = [kwh * 0.6 for kwh in day.LOAD]  # the evening needs less than a full battery
        plan = plan_for(prices, load, day.PV)
        bold = opt.optimize(day_slots(prices, load, day.PV), 15, battery())
        self.assertLess(plan.grid_target(), bold.grid_target() - 20)

    def test_shown_slots_are_the_forecast(self):
        day = fixture_spread
        plan = plan_for(stretched(day.PRICES, 2.5), day.LOAD, day.PV)
        self.assertAlmostEqual(sum(s.load for s in plan.slots), sum(day.LOAD), places=6)
        self.assertAlmostEqual(sum(s.pv for s in plan.slots), sum(day.PV) / 1000, places=6)
        self.assertAlmostEqual(plan.baseline_cost - plan.cost, plan.expected, delta=0.25)

    def test_sunny_day_needs_no_grid_charge(self):
        day = fixture_spread
        prices = stretched(day.PRICES, 2.5)
        sunny = [wh * 12 for wh in day.PV]  # 34 kWh: fills the battery by noon
        plan = plan_for(prices, day.LOAD, sunny)
        charged = sum(s.battery for s in plan.slots if s.mode == GRID_CHARGE)
        dull = plan_for(prices, day.LOAD, day.PV)
        self.assertLess(charged, 0.5 * sum(s.battery for s in dull.slots if s.mode == GRID_CHARGE))


class StressTest(unittest.TestCase):
    """Carry the plan out on days that differ from the forecast."""

    TRIALS = 200

    def realized(self, prices, load, pv, soc, rng):
        plan = plan_for(prices, load, pv, soc)
        if {s.mode for s in plan.slots} == {STANDARD}:
            return None
        results = []
        for _ in range(self.TRIALS):
            day_load, day_pv = rng.uniform(0.7, 1.3), rng.uniform(0.5, 1.5)
            slots = [
                opt.Slot(
                    i * 900.0, 0.25, prices[i],
                    load[i // 4] / 4 * day_load * rng.uniform(0.6, 1.4),
                    pv[i // 4] / 4000 * day_pv,
                )
                for i in range(96)
            ]
            efficiency = rng.uniform(0.65, 0.80)
            real = battery(
                charge_eff=efficiency, grid_eff=efficiency * 0.95,
                standby=rng.uniform(0.02, 0.05),
            )
            results.append(opt.replay(slots, soc, real, plan).savings)
        return results

    def test_plans_pay_on_days_that_differ_from_the_forecast(self):
        rng = random.Random(7)
        acted = 0
        for name, day in (("15 Nov", fixture_day), ("16 Nov", fixture_spread)):
            for stretch in (1.0, 1.5, 2.0, 3.0):
                for pv_scale in (1.0, 6.0, 12.0):
                    for soc in (15, 50):
                        prices = stretched(day.PRICES, stretch)
                        pv = [wh * pv_scale for wh in day.PV]
                        results = self.realized(prices, day.LOAD, pv, soc, rng)
                        if results is None:
                            continue
                        acted += 1
                        case = f"{name} x{stretch} pv x{pv_scale} soc {soc}"
                        mean = sum(results) / len(results)
                        losing = sum(r < -0.02 for r in results) / len(results)
                        self.assertGreater(mean, 0.10, case)
                        self.assertLess(losing, 0.05, case)
                        self.assertGreater(min(results), -0.15, case)
        self.assertGreaterEqual(acted, 5)  # the test must not pass by never acting

    def test_real_prices_of_both_days_leave_the_battery_alone(self):
        for day in (fixture_day, fixture_spread):
            plan = plan_for(day.PRICES, day.LOAD, day.PV)
            self.assertEqual({s.mode for s in plan.slots}, {STANDARD})


class AuditTest(unittest.TestCase):
    PRICES = [0.30] * 12 + [0.45] * 12
    HOME = [0.5] * 24
    NONE = [0.0] * 24

    def bat(self):
        return battery(standby=0.0)

    def test_a_day_in_standard_saves_nothing(self):
        saved = audit.day_result(self.HOME, self.NONE, self.HOME, self.NONE, self.PRICES, 15, 15, self.bat())
        self.assertAlmostEqual(saved, 0.0)

    def test_a_good_and_a_bad_deviation(self):
        # 3 kWh charged in the cheap half, used in the dear half.
        grid = [0.5 + 4.0 / 12] * 12 + [0.25] * 12
        good = audit.day_result(self.HOME, self.NONE, grid, self.NONE, self.PRICES, 15, 15, self.bat())
        self.assertAlmostEqual(good, 3.0 * 0.45 - 4.0 * 0.30)
        # Charged, but never used and gone by the next morning.
        grid = [0.5 + 4.0 / 12] * 12 + [0.5] * 12
        bad = audit.day_result(self.HOME, self.NONE, grid, self.NONE, self.PRICES, 15, 15, self.bat())
        self.assertAlmostEqual(bad, -4.0 * 0.30)

    def test_energy_left_in_the_battery_counts(self):
        grid = [0.5 + 4.0 / 12] * 12 + [0.5] * 12
        kept = audit.day_result(self.HOME, self.NONE, grid, self.NONE, self.PRICES, 15, 60, self.bat())
        self.assertAlmostEqual(kept, -4.0 * 0.30 + 0.45 * 6.62 * 0.30)

    def test_gaps_in_the_data_give_no_result(self):
        grid = [None] + self.HOME[1:]
        self.assertIsNone(
            audit.day_result(self.HOME, self.NONE, grid, self.NONE, self.PRICES, 15, 15, self.bat())
        )

    def test_model_error_is_taken_off_and_losses_pause_the_schedule(self):
        today = date(2026, 12, 1)
        ago = lambda n: today - timedelta(days=n)  # noqa: E731
        # Quiet days show the model 10 ct a day off; three active days gained 5 ct each.
        results = [(ago(n), 0.10, False) for n in range(4, 20)]
        results += [(ago(n), 0.05, True) for n in (1, 2, 3)]
        result = audit.summarize(results, today)
        self.assertAlmostEqual(result.bias, 0.10)
        self.assertEqual(result.active_days, 3)
        self.assertAlmostEqual(result.total, -0.15)
        self.assertTrue(result.losing)
        # Two weeks on the days have aged out: the schedule may act again.
        later = audit.summarize(results, today + timedelta(days=14))
        self.assertEqual(later.active_days, 0)
        self.assertFalse(later.losing)

    def test_two_bad_days_are_not_enough_to_tell(self):
        today = date(2026, 12, 1)
        results = [(today - timedelta(days=n), -0.50, True) for n in (1, 2)]
        self.assertFalse(audit.summarize(results, today).losing)
        results.append((today - timedelta(days=3), 2.00, True))
        self.assertFalse(audit.summarize(results, today).losing)  # in the black overall


if __name__ == "__main__":
    unittest.main()
