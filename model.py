"""Consumption models learned from the archive (no Home Assistant imports).

Three kinds of model predict the 24 hourly kWh of a day:

  recent       weekday x hour, recent weeks count more (the classic profile)
  seasonal     the same, but days of the same season, also of earlier years,
               count as much as recent ones
  temperature  per hour a straight line over the heating degrees of the day,
               plus what weekday and hour add on top. Follows a heat pump
               and also holds for colder days than any seen so far.

Which one is used is decided by a backtest: every model predicts each of the
last days from the data before it, and the one with the smallest error wins.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

RECENT = "recent"
SEASONAL = "seasonal"
TEMPERATURE = "temperature"
KINDS = [RECENT, SEASONAL, TEMPERATURE]

RECENT_HALF_LIFE = 14.0  # days
SEASONAL_HALF_LIFE = 365.0  # days
SEASON_WIDTH = 30.0  # days, standard deviation of the season window
MIN_WEIGHT = 0.5  # summed weight a weekday needs, else all days are used

# Temperature model
BASE_TEMPERATURES = (13.0, 15.0, 17.0, 19.0)  # deg C above which nothing is heated
PHYSICS_HALF_LIFE = 365.0  # days: how the house reacts to cold changes slowly
HABIT_HALF_LIFE = 28.0  # days: what weekday and hour add changes faster
HABIT_PRIOR = 2.0  # weekday samples worth of trust in the all-days value
MIN_TEMPERATURE_DAYS = 21
MIN_TEMPERATURE_SPAN = 5.0  # K between the coldest and warmest day

# Backtest
TEST_DAYS = 21
MIN_TRAIN_DAYS = 14
MIN_TEST_DAYS = 7
# To replace the model in use, another one must be better by this share of
# its error and by this much consumption; else noise would flip the choice.
SWITCH_MARGIN = 0.03
SWITCH_MIN_GAIN = 0.005


@dataclass(frozen=True)
class Day:
    """One day of the archive."""

    date: date
    hours: Sequence[float | None]  # 24 x kWh
    temp: float | None = None  # mean outdoor temperature, deg C


def _season_distance(a: date, b: date) -> float:
    """Days between two dates within the year (0..182.5)."""
    diff = abs(a.timetuple().tm_yday - b.timetuple().tm_yday)
    return min(diff, 365.25 - diff)


def predict_similar(
    days: Sequence[Day], target: date, half_life: float, season_width: float | None
) -> list[float] | None:
    """Weighted mean of earlier days per hour; None without any data.

    Days of the target's weekday are used if they carry enough weight,
    otherwise all days.
    """
    same = [[0.0, 0.0] for _ in range(24)]
    every = [[0.0, 0.0] for _ in range(24)]
    weekday = target.weekday()
    for day in days:
        age = (target - day.date).days
        if age <= 0:
            continue
        weight = 0.5 ** (age / half_life)
        if season_width:
            distance = _season_distance(day.date, target)
            weight *= math.exp(-(distance**2) / (2 * season_width**2))
        if weight < 1e-9:
            continue
        own = day.date.weekday() == weekday
        for h, value in enumerate(day.hours):
            if value is None or value < 0:
                continue
            every[h][0] += weight
            every[h][1] += weight * value
            if own:
                same[h][0] += weight
                same[h][1] += weight * value
    if any(total[0] <= 0 for total in every):
        return None
    return [
        same[h][1] / same[h][0] if same[h][0] >= MIN_WEIGHT else every[h][1] / every[h][0]
        for h in range(24)
    ]


@dataclass(frozen=True)
class TemperatureFit:
    """kWh per hour = intercept + slope x heating degrees + habit(weekday)."""

    base: float  # deg C above which the slope has no effect
    intercept: tuple[float, ...]  # 24 x kWh
    slope: tuple[float, ...]  # 24 x kWh per K
    habit: Mapping[int, tuple[float, ...]]  # weekday -> 24 x kWh on top
    days: int

    def predict(self, day: date, temp: float) -> list[float]:
        """Hourly kWh for a day with the given mean temperature."""
        degrees = max(self.base - temp, 0.0)
        habit = self.habit.get(day.weekday(), (0.0,) * 24)
        return [
            max(self.intercept[h] + self.slope[h] * degrees + habit[h], 0.0)
            for h in range(24)
        ]

    @property
    def daily_base(self) -> float:
        """kWh per day without heating."""
        habit = sum(sum(values) for values in self.habit.values()) / max(len(self.habit), 1)
        return sum(self.intercept) + habit

    @property
    def daily_slope(self) -> float:
        """Additional kWh per day for each K below the base temperature."""
        return sum(self.slope)


def _line(points: Sequence[tuple[float, float, float]]) -> tuple[float, float, float]:
    """Weighted least squares y = a + b x over (weight, x, y); returns a, b, error.

    A falling line (less consumption when colder) is noise: then b = 0.
    """
    sw = sx = sy = sxx = sxy = 0.0
    for w, x, y in points:
        sw += w
        sx += w * x
        sy += w * y
        sxx += w * x * x
        sxy += w * x * y
    if sw <= 0:
        return 0.0, 0.0, 0.0
    spread = sw * sxx - sx * sx
    slope = (sw * sxy - sx * sy) / spread if spread > 1e-9 else 0.0
    if slope < 0:
        slope = 0.0
    intercept = (sy - slope * sx) / sw
    error = sum(w * (y - intercept - slope * x) ** 2 for w, x, y in points)
    return intercept, slope, error


def fit_temperature(days: Sequence[Day], target: date) -> TemperatureFit | None:
    """Fit the temperature model to the days before `target`; None if they do not allow it."""
    usable = [
        d for d in days if d.temp is not None and (target - d.date).days > 0
    ]
    if len(usable) < MIN_TEMPERATURE_DAYS:
        return None
    temps = [d.temp for d in usable]
    if max(temps) - min(temps) < MIN_TEMPERATURE_SPAN:
        return None
    physics = [0.5 ** ((target - d.date).days / PHYSICS_HALF_LIFE) for d in usable]

    # Base temperature: the one whose line explains the daily totals best.
    totals = [
        (w, d.temp, sum(v for v in d.hours if v is not None))
        for w, d in zip(physics, usable, strict=True)
        if all(v is not None for v in d.hours)
    ]
    if len(totals) < MIN_TEMPERATURE_DAYS:
        return None
    base = min(
        BASE_TEMPERATURES,
        key=lambda b: _line([(w, max(b - t, 0.0), total) for w, t, total in totals])[2],
    )
    degrees = [max(base - d.temp, 0.0) for d in usable]

    intercept, slope = [], []
    for h in range(24):
        a, b, _ = _line(
            [
                (w, x, d.hours[h])
                for w, x, d in zip(physics, degrees, usable, strict=True)
                if d.hours[h] is not None
            ]
        )
        intercept.append(a)
        slope.append(b)

    # What is left over: by weekday, pulled towards the value of all days so
    # a few noisy samples do not shape the forecast.
    every = [[0.0, 0.0] for _ in range(24)]
    by_weekday = {wd: [[0.0, 0.0] for _ in range(24)] for wd in range(7)}
    for d, x in zip(usable, degrees, strict=True):
        weight = 0.5 ** ((target - d.date).days / HABIT_HALF_LIFE)
        own = by_weekday[d.date.weekday()]
        for h, value in enumerate(d.hours):
            if value is None:
                continue
            rest = value - intercept[h] - slope[h] * x
            every[h][0] += weight
            every[h][1] += weight * rest
            own[h][0] += weight
            own[h][1] += weight * rest
    common = [total[1] / total[0] if total[0] > 0 else 0.0 for total in every]
    habit = {
        wd: tuple(
            (sums[h][1] + HABIT_PRIOR * common[h]) / (sums[h][0] + HABIT_PRIOR)
            for h in range(24)
        )
        for wd, sums in by_weekday.items()
    }
    return TemperatureFit(base, tuple(intercept), tuple(slope), habit, len(usable))


def predict(
    kind: str,
    days: Sequence[Day],
    target: date,
    temp: float | None,
    fit: TemperatureFit | None = None,
) -> list[float] | None:
    """Hourly kWh of `target` by one kind of model; None if it cannot tell.

    For the temperature model an existing fit is reused, else one is made.
    """
    if kind == RECENT:
        return predict_similar(days, target, RECENT_HALF_LIFE, None)
    if kind == SEASONAL:
        return predict_similar(days, target, SEASONAL_HALF_LIFE, SEASON_WIDTH)
    if temp is None:
        return None
    fit = fit or fit_temperature(days, target)
    return fit.predict(target, temp) if fit else None


@dataclass(frozen=True)
class Backtest:
    """How well each kind of model predicted the last days."""

    hourly: dict[str, float]  # share: sum of hourly errors / consumption
    daily: dict[str, float]  # share: error of the day's total / consumption
    days: int  # days every listed model was tested on


def backtest(days: Sequence[Day], kinds: Sequence[str] = KINDS) -> Backtest:
    """Let every model predict each of the last days from the days before it."""
    days = sorted(days, key=lambda d: d.date)
    # Per kind and day: (hourly error, error of the total, consumption)
    results: dict[str, dict[date, tuple[float, float, float]]] = {k: {} for k in kinds}
    for index in range(max(len(days) - TEST_DAYS, MIN_TRAIN_DAYS), len(days)):
        day = days[index]
        if any(v is None for v in day.hours):
            continue
        earlier = days[:index]
        actual = sum(day.hours)
        for kind in kinds:
            forecast = predict(kind, earlier, day.date, day.temp)
            if forecast is None:
                continue
            results[kind][day.date] = (
                sum(abs(f - a) for f, a in zip(forecast, day.hours, strict=True)),
                abs(sum(forecast) - actual),
                actual,
            )
    tested = {k: r for k, r in results.items() if len(r) >= MIN_TEST_DAYS}
    if not tested:
        return Backtest({}, {}, 0)
    common = set.intersection(*(set(r) for r in tested.values()))
    if len(common) < MIN_TEST_DAYS:
        # The models cover different days: compare those that cover the most.
        most = max(len(r) for r in tested.values())
        tested = {k: r for k, r in tested.items() if len(r) == most}
        common = set.intersection(*(set(r) for r in tested.values()))
    hourly, daily = {}, {}
    for kind, result in tested.items():
        consumed = sum(result[d][2] for d in common)
        if consumed <= 0:
            continue
        hourly[kind] = sum(result[d][0] for d in common) / consumed
        daily[kind] = sum(result[d][1] for d in common) / consumed
    return Backtest(hourly, daily, len(common))


def choose(errors: Mapping[str, float], current: str | None) -> str:
    """Kind of model to use: the best one, if it clearly beats the current one.

    Without a current model the classic profile is the one to beat.
    """
    if not errors:
        return RECENT
    best = min(errors, key=lambda kind: errors[kind])
    if current not in errors:
        current = RECENT
    if current in errors and (
        errors[best] > errors[current] * (1 - SWITCH_MARGIN)
        or errors[current] - errors[best] < SWITCH_MIN_GAIN
    ):
        return current
    return best


def measured_error(
    pairs: Sequence[tuple[date, Sequence[float | None], Sequence[float | None]]],
) -> tuple[float, float, int] | None:
    """Error of stored forecasts against what happened.

    pairs: (day, actual hours, forecast hours). Returns the share the day's
    total was off, the share the hours were off, and the number of days.
    """
    daily = hourly = consumed = 0.0
    count = 0
    for _, actual, forecast in pairs:
        if any(v is None for v in actual) or any(v is None for v in forecast):
            continue
        daily += abs(sum(forecast) - sum(actual))
        hourly += sum(abs(f - a) for f, a in zip(forecast, actual, strict=True))
        consumed += sum(actual)
        count += 1
    if not count or consumed <= 0:
        return None
    return daily / consumed, hourly / consumed, count
