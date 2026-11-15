"""Six real weeks: daily consumption and mean outdoor temperature, autumn 2026.

Consumption rises from about 7 to 38 kWh a day while it gets colder (heat
pump). The hourly shape is the same for every day (SHAPE): the fixture checks
how well the models follow the level, not the time of day.
"""

from datetime import date, timedelta

FIRST = date(2026, 10, 5)

# kWh per day from FIRST on
CONSUMPTION = [
    7.005, 9.138, 7.729, 10.811, 5.346, 7.129, 13.011, 17.059, 17.038, 13.593,
    16.206, 13.577, 10.131, 14.655, 15.022, 18.400, 18.221, 21.960, 20.129, 21.086,
    21.047, 21.913, 14.157, 11.822, 18.990, 19.234, 25.700, 19.401, 22.532, 19.316,
    23.454, 22.824, 31.346, 25.073, 30.303, 22.621, 21.834, 21.126, 21.553, 32.321,
    38.107,
]

# deg C, daily mean, from FIRST on
TEMPERATURE = [
    17.17, 16.58, 18.53, 15.91, 11.76, 14.94, 13.01, 12.52, 12.65, 16.05,
    12.79, 14.70, 13.88, 11.04, 13.94, 11.45, 12.26, 12.29, 13.54, 11.61,
    11.55, 12.57, 14.17, 13.21, 9.52, 9.76, 9.97, 10.47, 8.99, 9.12,
    9.94, 8.51, 6.66, 7.92, 9.55, 9.67, 9.64, 11.02, 10.73, 6.38,
    5.28,
]

# Share of the day's consumption per hour
_PROFILE = [
    0.594, 0.643, 1.053, 1.538, 1.035, 0.711, 0.608, 0.671, 0.746, 0.816, 0.756, 0.749,
    0.870, 0.786, 0.828, 0.937, 1.189, 0.861, 0.950, 1.260, 0.716, 0.763, 0.853, 0.779,
]
SHAPE = [value / sum(_PROFILE) for value in _PROFILE]


def days(day_type):
    """The weeks as a list of `day_type(date, hours, temp)`."""
    return [
        day_type(FIRST + timedelta(days=i), [kwh * share for share in SHAPE], temp)
        for i, (kwh, temp) in enumerate(zip(CONSUMPTION, TEMPERATURE, strict=True))
    ]
