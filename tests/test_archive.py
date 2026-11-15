"""Permanent archive of daily records."""

import unittest
from datetime import UTC, date, timedelta

from _loader import load

archive = load("archive")
Archive = archive.Archive

TODAY = date(2026, 11, 15)


def filled(days, start=TODAY - timedelta(days=1)):
    a = Archive()
    for i in range(days):
        a.set_energy(start - timedelta(days=i), [0.5] * 24, [0.1] * 24)
    return a


class ArchiveTest(unittest.TestCase):
    def test_store_and_read_back(self):
        a = Archive()
        a.set_energy(date(2026, 11, 14), [1.23456789] + [None] * 23, None)
        a.set_temp(date(2026, 11, 14), 5.276)
        self.assertTrue(a.dirty)
        b = Archive(a.as_dict())
        self.assertFalse(b.dirty)
        self.assertEqual(b.count(), 1)
        self.assertEqual(b.temp(date(2026, 11, 14)), 5.28)
        rows = b.rows(UTC)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].isoformat(), "2026-11-14T00:00:00+00:00")
        self.assertEqual(rows[0][1], 1.2346)
        self.assertEqual(b.rows(UTC, archive.SOLAR), [])

    def test_pending_fills_gaps_first_then_goes_back(self):
        a = filled(3)  # 14., 13., 12.
        del a.days["2026-11-13"]
        self.assertEqual(
            a.pending(TODAY, 4),
            [date(2026, 11, 13), date(2026, 11, 11), date(2026, 11, 10), date(2026, 11, 9)],
        )

    def test_a_day_with_only_a_forecast_is_still_fetched(self):
        a = Archive()
        a.set_snapshot(date(2026, 11, 14), archive.PRED, [0.5] * 24)
        a.set_temp(date(2026, 11, 14), 5.0)
        self.assertFalse(a.known(date(2026, 11, 14)))
        self.assertEqual(a.pending(TODAY, 1), [date(2026, 11, 14)])

    def test_backfill_stops_after_two_empty_weeks(self):
        a = filled(5)
        day = TODAY - timedelta(days=6)
        for _ in range(archive.EMPTY_DAYS_END - 1):
            a.set_energy(day, None, None)
            day -= timedelta(days=1)
        a.check_complete()
        self.assertFalse(a.complete)
        self.assertEqual(a.pending(TODAY, 1), [day])
        a.set_energy(day, None, None)
        a.check_complete()
        self.assertTrue(a.complete)
        self.assertEqual(a.pending(TODAY, 5), [])
        self.assertEqual(a.count(), 5)
        self.assertEqual(a.oldest(), TODAY - timedelta(days=5))
        # A missed day in between is still fetched later.
        del a.days[(TODAY - timedelta(days=2)).isoformat()]
        self.assertEqual(a.pending(TODAY, 5), [TODAY - timedelta(days=2)])

    def test_legacy_cache_is_taken_over(self):
        a = Archive.from_legacy(
            {"2026-11-14": [0.5] * 24, "2026-11-13": None, "broken": [1.0]}
        )
        self.assertEqual(a.count(), 1)
        self.assertTrue(a.known(date(2026, 11, 13)))
        self.assertEqual(a.pending(TODAY, 1), [date(2026, 11, 12)])

    def test_first_snapshot_of_a_day_stays(self):
        a = filled(2)
        day = date(2026, 11, 14)
        self.assertTrue(a.set_snapshot(day, archive.PV_FORECAST, [0.2] * 24))
        self.assertFalse(a.set_snapshot(day, archive.PV_FORECAST, [0.9] * 24))
        self.assertFalse(a.set_snapshot(date(2026, 11, 13), archive.PV_FORECAST, [None] * 24))
        pairs = a.pairs(archive.SOLAR, archive.PV_FORECAST)
        self.assertEqual([(d, f[0]) for d, _, f in pairs], [(day, 0.2)])

    def test_old_snapshots_are_dropped_but_energy_stays(self):
        a = filled(200)
        old = TODAY - timedelta(days=150)
        new = TODAY - timedelta(days=30)
        for day in (old, new):
            a.set_snapshot(day, archive.PRED, [0.5] * 24)
            a.set_snapshot(day, archive.PV_FORECAST, [0.1] * 24)
        a.prune_snapshots(TODAY)
        self.assertIsNone(a.snapshot(old, archive.PRED))
        self.assertIsNone(a.snapshot(old, archive.PV_FORECAST))
        self.assertIsNotNone(a.snapshot(new, archive.PRED))
        self.assertEqual(a.count(), 200)

    def test_days_without_temperature(self):
        a = filled(3)
        a.set_temp(date(2026, 11, 13), 8.0)
        self.assertEqual(a.days_without_temp(), [date(2026, 11, 12), date(2026, 11, 14)])


if __name__ == "__main__":
    unittest.main()
