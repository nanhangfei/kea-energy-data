"""The EV what-ifs: where the charging lands and what it costs on each plan."""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "decisions"))

import build_ev_scenarios as m  # noqa: E402
import build_plan_recommendations as plans  # noqa: E402
import estimate_common as common  # noqa: E402

WEEK = frozenset(range(1, 8))
UTC = dt.UTC
PEAK, NIGHT = Decimal("0.32"), Decimal("0.15")


def energy_band(label, start, end, rate):
    return plans.Band("energy", label, WEEK, start, end, Decimal(rate))


FLAT = plans.Tariff(name="flat", bands=(energy_band("ANY", 0, 1440, "0.27"),),
                    export_bands=(energy_band("ANY", 0, 1440, "0.08"),))
TOU = plans.Tariff(name="tou", bands=(
    energy_band("NIGHT", 0, 420, str(NIGHT)),
    energy_band("PEAK", 420, 1380, str(PEAK)),
    energy_band("NIGHT", 1380, 1440, str(NIGHT)),
), export_bands=(energy_band("ANY", 0, 1440, "0.08"),))
# A plan that buys back above its own night rate. Rare, but it is the shape
# in which charging into your own sun LOSES, and the loss has to be priced.
BUYBACK = plans.Tariff(name="buyback", bands=TOU.bands,
                       export_bands=(energy_band("ANY", 0, 1440, "0.20"),))


def half_hours(days=2, kwh=0.2, export_at=None, offset=720):
    """`days` local days from 2025-06-02 (a Monday), NZST."""
    start = dt.datetime(2025, 6, 2)
    out = []
    for k in range(48 * days):
        loc = start + dt.timedelta(minutes=30 * k)
        exp = 1.0 if export_at is not None and loc.time() == export_at else 0.0
        out.append(common.HalfHour(
            at=(loc - dt.timedelta(minutes=offset)).replace(tzinfo=UTC),
            loc=loc, utc_offset_min=offset, import_kwh=kwh, export_kwh=exp))
    return out


def session(local_start: dt.datetime, n: int, kwh: float) -> m.Session:
    at = (local_start - dt.timedelta(hours=12)).replace(tzinfo=UTC)
    night = dt.datetime.combine(local_start.date(), dt.time(23)) - dt.timedelta(hours=12)
    return m.Session(at, n, kwh, local_start.time(), night.replace(tzinfo=UTC))


class TestAddEv(unittest.TestCase):
    def test_a_session_is_full_rate_then_the_remainder(self):
        self.assertEqual(m.session_slots(5.0, 7.2), [3.6, 1.4])
        self.assertEqual(m.session_slots(0.0, 7.2), [])

    def test_a_night_session_runs_past_midnight_onto_the_next_date(self):
        hh = half_hours(days=2)
        p, charged = m.add_daily_charging(hh, dt.time(23), [3.6, 3.6, 1.0])
        # Monday 00:00 gets the tail of Sunday's session; Tuesday's tail is
        # past the data. Two dates, two whole sessions.
        self.assertAlmostEqual(charged, 2 * 8.2)
        cells = {(r.at.isoweekday(), r.at.hour, r.at.minute): float(r.kwh)
                 for r in p.readings() if not r.exported}
        self.assertAlmostEqual(cells[(1, 23, 0)], 3.8)
        self.assertAlmostEqual(cells[(2, 0, 0)], 1.2)            # Monday's third slot
        self.assertAlmostEqual(cells[(2, 23, 30)], 3.8)
        self.assertAlmostEqual(cells[(1, 0, 0)], 1.2)            # Sunday's tail

    def test_charging_first_uses_what_would_have_been_exported(self):
        for got, want in ((m.charge_half_hour(0.2, 1.0, 3.6), (2.8, 0.0, 1.0)),
                          (m.charge_half_hour(0.2, 1.0, 0.5), (0.2, 0.5, 0.5))):
            for g, w in zip(got, want, strict=True):
                self.assertAlmostEqual(g, w)
        hh = half_hours(days=1, export_at=dt.time(18))
        p, _ = m.add_daily_charging(hh, dt.time(18), [3.6])
        self.assertEqual(p.total(exported=True), 0.0)
        self.assertAlmostEqual(p.total(exported=False), 0.2 * 48 + 2.6)


def with_export(hh, exports):
    """The same half-hours, with the export register reading at these times."""
    return [h._replace(export_kwh=exports.get(h.loc.time(), 0.0)) for h in hh]


class TestSolarSoak(unittest.TestCase):
    """Charging into the half-hours the export register actually recorded."""

    def cells(self, profile, exported=False):
        return {(r.at.hour, r.at.minute): float(r.kwh)
                for r in profile.readings() if r.exported == exported}

    def test_the_session_goes_into_the_biggest_exporters_first(self):
        hh = with_export(half_hours(days=1), {dt.time(11): 4.0,
                                              dt.time(11, 30): 2.0,
                                              dt.time(12): 0.5})
        p, charged = m.add_soak_charging(hh, dt.time(18), [3.6, 3.6, 1.0])
        self.assertAlmostEqual(charged, 8.2)
        c = self.cells(p)
        # 11:00 takes a whole slot and still exports 0.4; 11:30 takes a slot
        # and imports the rest; 12:00 takes what is left of the session.
        self.assertAlmostEqual(c[(11, 0)], 0.2)
        self.assertAlmostEqual(self.cells(p, exported=True)[(11, 0)], 0.4)
        self.assertAlmostEqual(c[(11, 30)], 1.8)
        self.assertAlmostEqual(c[(12, 0)], 0.7)
        self.assertAlmostEqual(c[(18, 0)], 0.2)             # nothing fell back

    def test_what_the_roof_cannot_cover_falls_back_to_the_evening(self):
        hh = with_export(half_hours(days=1), {dt.time(11): 1.0})
        p, charged = m.add_soak_charging(hh, dt.time(18), [3.6, 3.6, 1.0])
        self.assertAlmostEqual(charged, 8.2)
        c = self.cells(p)
        # ⚠️ add_ev soaks at the WALLBOX rate, not at the export: a car plugged
        # in at 11:00 draws 7.2 kW whether or not the roof is keeping up, and
        # 2.6 of that slot is bought.
        self.assertAlmostEqual(c[(11, 0)], 2.8)
        self.assertAlmostEqual(c[(18, 0)], 3.8)
        self.assertAlmostEqual(c[(18, 30)], 1.2)
        # The same kWh a day as the evening baseline, so the bills compare.
        _, evening = m.add_daily_charging(hh, dt.time(18), [3.6, 3.6, 1.0])
        self.assertAlmostEqual(charged, evening)

    def test_a_day_that_never_exported_charges_nothing_into_the_sun(self):
        hh = half_hours(days=1)
        soaked, charged = m.add_soak_charging(hh, dt.time(18), [3.6, 1.0])
        evening, _ = m.add_daily_charging(hh, dt.time(18), [3.6, 1.0])
        self.assertAlmostEqual(charged, 4.6)
        self.assertEqual(self.cells(soaked), self.cells(evening))

    def session_day(self, exports):
        """One day: a 3.6 kWh session at 18:00, and these exports."""
        hh = [h._replace(import_kwh=2.0) if h.loc.hour == 18 else h
              for h in half_hours(days=1)]
        return with_export(hh, exports), [session(dt.datetime(2025, 6, 2, 18), 2, 3.6)]

    def test_a_moved_session_never_takes_more_than_that_half_hour_exported(self):
        hh, sessions = self.session_day({dt.time(11): 2.0, dt.time(11, 30): 0.5})
        p, charged = m.soak_sessions(hh, sessions)
        # Nothing is dropped, only relocated: as_detected charges the same.
        _, detected = m.shift_sessions(hh, sessions, to_night=False)
        self.assertAlmostEqual(charged, detected)
        c = self.cells(p)
        # 2.5 of the 3.6 displaced an export and cost no new import at all.
        self.assertAlmostEqual(c[(11, 0)], 0.2)
        self.assertAlmostEqual(c[(11, 30)], 0.2)
        self.assertEqual(p.total(exported=True), 0.0)
        # The 1.1 that would not fit stayed where the meter found it, split
        # in the proportions it was taken off.
        self.assertAlmostEqual(c[(18, 0)], 0.75)
        self.assertAlmostEqual(c[(18, 30)], 0.75)

    def test_a_session_that_fits_leaves_its_own_half_hours_empty(self):
        hh, sessions = self.session_day({dt.time(11): 4.0})
        p, _ = m.soak_sessions(hh, sessions)
        c = self.cells(p)
        self.assertAlmostEqual(c[(18, 0)], 0.2)
        self.assertAlmostEqual(c[(18, 30)], 0.2)
        self.assertAlmostEqual(self.cells(p, exported=True)[(11, 0)], 0.4)

    def test_a_losing_solar_soak_is_priced_and_published_anyway(self):
        # A plan that pays 0.20 to export and charges 0.15 at night: soaking
        # gives up more credit than it saves, and the honest answer is "your
        # night rate still wins". The row is published saying so.
        hh = [h._replace(import_kwh=2.0) if h.loc.hour == 1 else h
              for h in with_export(half_hours(days=1), {dt.time(11): 1.0})]
        sessions = [session(dt.datetime(2025, 6, 2, 1), 2, 3.6)]
        removed, _ = m.shift_sessions(hh, sessions, to_night=None)
        base, soak = m.scenario_rows("shift_detected", [1], {1: BUYBACK}, removed, [
            ("as_detected", m.shift_sessions(hh, sessions, to_night=False)),
            ("solar_soak", m.soak_sessions(hh, sessions)),
        ])
        self.assertEqual(base["saving"], 0)
        self.assertLess(soak["saving"], 0)
        self.assertEqual(soak["annual_ev_kwh"], base["annual_ev_kwh"])

    def test_the_session_query_keeps_the_runs_the_panels_hid(self):
        # detect-v2 publishes a PV-shaved wallbox with no class. Filtering on
        # 'high_power' alone would withdraw EV advice from exactly the
        # households solar_soak exists for; an unmasked NULL is not an EV
        # claim and stays out.
        self.assertIn("s.charger_class = 'high_power'", m.SESSIONS_SQL)
        self.assertIn("s.charger_class is null and s.masked_half_hours > 0",
                      m.SESSIONS_SQL)


class TestShift(unittest.TestCase):
    def setUp(self):
        self.hh = half_hours(days=1, kwh=0.2)
        # A 3.6 kWh evening session on top of the 0.2 base, 18:00-19:00.
        self.hh = [h._replace(import_kwh=h.import_kwh + 1.8)
                   if h.loc.hour == 18 else h for h in self.hh]
        self.sessions = [session(dt.datetime(2025, 6, 2, 18), 2, 3.6)]

    def cells(self, profile):
        return {(r.at.hour, r.at.minute): float(r.kwh)
                for r in profile.readings() if not r.exported}

    def test_night_moves_the_low_bound_to_23_00_the_same_night(self):
        p, charged = m.shift_sessions(self.hh, self.sessions, to_night=True)
        c = self.cells(p)
        self.assertAlmostEqual(c[(18, 0)], 0.2)
        self.assertAlmostEqual(c[(23, 0)], 2.0)
        self.assertAlmostEqual(c[(23, 30)], 2.0)
        self.assertAlmostEqual(charged, 3.6)
        self.assertAlmostEqual(p.total(exported=False), 0.2 * 48 + 3.6)

    def test_removed_and_as_detected(self):
        removed, none = m.shift_sessions(self.hh, self.sessions, to_night=None)
        self.assertAlmostEqual(removed.total(exported=False), 0.2 * 48)
        self.assertEqual(none, 0.0)
        detected, charged = m.shift_sessions(self.hh, self.sessions, to_night=False)
        self.assertAlmostEqual(self.cells(detected)[(18, 30)], 2.0)
        self.assertEqual(charged, 3.6)

    def test_what_is_moved_is_what_was_taken_off(self):
        # A session that tails off: 4.0 kWh spread evenly is 2.0 a
        # half-hour, but the second half-hour imported only 0.6.
        hh = [h._replace(import_kwh={dt.time(18): 3.8, dt.time(18, 30): 0.6}
                         .get(h.loc.time(), 0.2)) for h in half_hours(days=1)]
        sessions = [session(dt.datetime(2025, 6, 2, 18), 2, 4.0)]
        _, moved = m.removable(hh, sessions)
        self.assertAlmostEqual(moved[0], 2.0 + 0.6)
        detected, charged_detected = m.shift_sessions(hh, sessions, to_night=False)
        night, charged_night = m.shift_sessions(hh, sessions, to_night=True)
        self.assertAlmostEqual(night.total(exported=False),
                               detected.total(exported=False))
        self.assertAlmostEqual(charged_night, charged_detected)
        self.assertNotIn((18, 30), self.cells(night))               # all of it moved
        removed, _ = m.shift_sessions(hh, sessions, to_night=None)
        base, moved_row = m.scenario_rows("shift_detected", [1], {1: FLAT}, removed, [
            ("as_detected", (detected, charged_detected)),
            ("night", (night, charged_night))])
        self.assertEqual(moved_row["saving"], 0)
        self.assertEqual(base["annual_ev_cost"], moved_row["annual_ev_cost"])

    def test_a_session_already_in_the_night_window_stays(self):
        sessions = [session(dt.datetime(2025, 6, 2, 1), 2, 1.0)]
        p, _ = m.shift_sessions(self.hh, sessions, to_night=True)
        self.assertAlmostEqual(self.cells(p)[(1, 0)], 0.2)
        self.assertAlmostEqual(p.total(exported=False), 0.2 * 48 + 3.6)

    def rows(self, tariff_id):
        removed, _ = m.shift_sessions(self.hh, self.sessions, to_night=None)
        return m.scenario_rows(
            "shift_detected", [tariff_id], {1: FLAT, 2: TOU}, removed, [
                ("as_detected", m.shift_sessions(self.hh, self.sessions, to_night=False)),
                ("night", m.shift_sessions(self.hh, self.sessions, to_night=True)),
            ])

    def test_a_flat_plan_saves_nothing_by_shifting(self):
        base, night = self.rows(1)
        self.assertEqual((base["saving"], night["saving"]), (0, 0))
        self.assertEqual(base["annual_ev_cost"], night["annual_ev_cost"])
        self.assertEqual(base["annual_ev_cost"], round(Decimal("3.6") * Decimal("0.27")
                                                       * Decimal("1.15"), 2))

    def test_time_of_use_saves_the_peak_to_night_difference(self):
        base, night = self.rows(2)
        self.assertEqual(base["saving"], 0)
        self.assertEqual(night["saving"],
                         round(Decimal("3.6") * (PEAK - NIGHT) * Decimal("1.15"), 2))
        self.assertEqual(night["annual_ev_kwh"], Decimal("3.6"))

    def test_time_of_use_is_found_by_its_catalogue_code(self):
        active = {1: {"plan_code": "KEA_ANY_VECT_STA"},
                  2: {"plan_code": "KEA_TOU_VECT_STA"},
                  3: {"plan_code": "KEA_WKND_VECT_STA"}}
        self.assertEqual(m.time_of_use_plan([1, 2, 3], active), 2)
        self.assertIsNone(m.time_of_use_plan([1, 3], active))


if __name__ == "__main__":
    unittest.main()
