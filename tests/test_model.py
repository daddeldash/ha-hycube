"""Consumption models and their selection."""

import math
import random
import unittest
from datetime import date, timedelta

import fixture_weeks
from _loader import load

model = load("model")
Day = model.Day

END = date(2026, 11, 15)
# Night-time hot water, a morning and an evening peak.
SHAPE = [0.6, 0.6, 1.0, 1.5, 1.0, 0.7, 0.6, 0.7, 0.8, 0.8, 0.8, 0.8,
         0.9, 0.8, 0.8, 0.9, 1.2, 0.9, 1.0, 1.3, 0.7, 0.8, 0.9, 0.8]


def series(count, daily, temp=None, noise=0.0, seed=1):
    """`count` days up to the day before END; daily(date, temp) -> kWh."""
    rng = random.Random(seed)
    out = []
    for i in range(count, 0, -1):
        day = END - timedelta(days=i)
        t = temp(day) if temp else None
        level = daily(day, t) / sum(SHAPE)
        out.append(Day(day, [level * s * (1 + rng.uniform(-noise, noise)) for s in SHAPE], t))
    return out


def heat_pump(day, t):
    return 8.0 + 2.5 * max(15.0 - t, 0.0)


def autumn(day):
    """Getting colder: 18 deg C two months ago, 6 deg C now, with weather on top."""
    age = (END - day).days
    return 6.0 + age * 0.2 + 2.5 * math.sin(age * 1.3)


def year_cycle(day):
    return 10.0 - 9.0 * math.cos((day.timetuple().tm_yday - 15) / 365.25 * 2 * math.pi)


class TemperatureModelTest(unittest.TestCase):
    def test_finds_base_load_and_slope(self):
        fit = model.fit_temperature(series(60, heat_pump, autumn), END)
        self.assertEqual(fit.base, 15.0)
        self.assertAlmostEqual(fit.daily_base, 8.0, delta=0.3)
        self.assertAlmostEqual(fit.daily_slope, 2.5, delta=0.1)

    def test_predicts_a_colder_day_than_any_seen(self):
        days = series(60, heat_pump, autumn)
        self.assertGreater(min(d.temp for d in days), 3.0)
        forecast = model.predict(model.TEMPERATURE, days, END, -5.0)
        self.assertAlmostEqual(sum(forecast), heat_pump(END, -5.0), delta=2.0)
        # The time-of-day shape survives.
        self.assertEqual(max(range(24), key=lambda h: forecast[h]), 3)

    def test_needs_enough_days_and_a_range_of_temperatures(self):
        self.assertIsNone(model.fit_temperature(series(20, heat_pump, autumn), END))
        self.assertIsNone(model.fit_temperature(series(60, heat_pump, lambda d: 10.0), END))
        self.assertIsNone(model.predict(model.TEMPERATURE, series(60, heat_pump, autumn), END, None))

    def test_consumption_never_falls_with_cold(self):
        # Noise could tilt single hours the wrong way.
        fit = model.fit_temperature(series(40, heat_pump, autumn, noise=0.6), END)
        self.assertTrue(all(s >= 0 for s in fit.slope))
        self.assertTrue(all(v >= 0 for v in fit.predict(END, 30.0)))

    def test_weekday_habit_on_top_of_temperature(self):
        def daily(day, t):
            return heat_pump(day, t) + (6.0 if day.weekday() == 5 else 0.0)  # Saturdays

        days = series(120, daily, autumn)
        saturday = END + timedelta(days=(5 - END.weekday()) % 7)
        monday = saturday + timedelta(days=2)
        fit = model.fit_temperature(days, END)
        extra = sum(fit.predict(saturday, 10.0)) - sum(fit.predict(monday, 10.0))
        # Most of it is found; the rest is held back as possible noise.
        self.assertGreater(extra, 4.0)
        self.assertLess(extra, 6.5)


class SimilarDaysTest(unittest.TestCase):
    def test_recent_follows_the_weekday(self):
        days = series(56, lambda day, t: 30.0 if day.weekday() >= 5 else 10.0)
        sunday = END  # 15 November 2026
        self.assertEqual(sunday.weekday(), 6)
        self.assertAlmostEqual(sum(model.predict(model.RECENT, days, sunday, None)), 30.0, places=3)
        monday = END + timedelta(days=1)
        self.assertAlmostEqual(sum(model.predict(model.RECENT, days, monday, None)), 10.0, places=3)

    def test_few_days_fall_back_to_all_days(self):
        days = series(3, lambda day, t: 12.0)
        self.assertAlmostEqual(sum(model.predict(model.RECENT, days, END, None)), 12.0, places=3)
        self.assertIsNone(model.predict(model.RECENT, [], END, None))

    def test_seasonal_remembers_last_year(self):
        # Two years with a yearly cycle; the weather just turned.
        days = series(760, lambda day, t: 5.0 + 2.0 * (20.0 - year_cycle(day)))
        target = date(2026, 12, 15)
        truth = 5.0 + 2.0 * (20.0 - year_cycle(target))
        days = [d for d in days if d.date < END]
        recent = sum(model.predict(model.RECENT, days, target, None))
        seasonal = sum(model.predict(model.SEASONAL, days, target, None))
        self.assertLess(abs(seasonal - truth), abs(recent - truth))


class BacktestTest(unittest.TestCase):
    def test_temperature_model_wins_for_a_heat_pump(self):
        result = model.backtest(series(60, heat_pump, autumn, noise=0.2))
        self.assertEqual(result.days, model.TEST_DAYS)
        self.assertEqual(model.choose(result.hourly, model.RECENT), model.TEMPERATURE)
        self.assertLess(result.daily[model.TEMPERATURE], result.daily[model.RECENT] / 2)

    def test_real_weeks(self):
        result = model.backtest(fixture_weeks.days(Day))
        self.assertLess(result.daily[model.TEMPERATURE], 0.15)
        self.assertGreater(result.daily[model.RECENT], 0.25)
        self.assertEqual(model.choose(result.hourly, model.RECENT), model.TEMPERATURE)

    def test_without_temperature_only_the_other_models_compete(self):
        result = model.backtest(series(60, lambda day, t: 12.0, noise=0.2))
        self.assertEqual(set(result.hourly), {model.RECENT, model.SEASONAL})

    def test_too_little_data_keeps_the_classic_profile(self):
        result = model.backtest(series(10, heat_pump, autumn))
        self.assertEqual(result.hourly, {})
        self.assertEqual(model.choose(result.hourly, None), model.RECENT)

    def test_incomplete_days_are_not_scored(self):
        days = series(40, lambda day, t: 12.0)
        days[-1] = Day(days[-1].date, [None] * 24, None)
        self.assertEqual(model.backtest(days).days, model.TEST_DAYS - 1)

    def test_a_small_lead_does_not_change_the_model(self):
        errors = {model.RECENT: 0.200, model.SEASONAL: 0.196}
        self.assertEqual(model.choose(errors, model.RECENT), model.RECENT)
        self.assertEqual(model.choose(errors, None), model.RECENT)
        errors[model.SEASONAL] = 0.150
        self.assertEqual(model.choose(errors, model.RECENT), model.SEASONAL)
        self.assertEqual(model.choose(errors, None), model.SEASONAL)
        # Next to no error at all: nothing to gain.
        tiny = {model.RECENT: 0.0038, model.SEASONAL: 0.0030}
        self.assertEqual(model.choose(tiny, model.RECENT), model.RECENT)
        # The current model is no longer available: take the best.
        self.assertEqual(model.choose({model.RECENT: 0.3}, model.TEMPERATURE), model.RECENT)


class MeasuredErrorTest(unittest.TestCase):
    def test_error_of_stored_forecasts(self):
        day = date(2026, 11, 14)
        pairs = [
            (day, [1.0] * 24, [1.5] * 12 + [0.5] * 12),  # total right, hours off
            (day, [1.0] * 24, [1.25] * 24),  # 25 % too high
            (day, [None] * 24, [1.0] * 24),  # no data
        ]
        daily, hourly, count = model.measured_error(pairs)
        self.assertEqual(count, 2)
        self.assertAlmostEqual(daily, 6.0 / 48.0)
        self.assertAlmostEqual(hourly, 18.0 / 48.0)
        self.assertIsNone(model.measured_error([]))


if __name__ == "__main__":
    unittest.main()
