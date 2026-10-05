"""Tests for planted.py. No database, no dataset: cohort.json, the calendar and
hand-made loads."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import planted as m  # noqa: E402
import uk_to_nz as tz  # noqa: E402

SPEC = json.loads((HERE / "cohort.json").read_text())
COHORT = SPEC["households"]
START, END = (dt.date.fromisoformat(d) for d in SPEC["nz_span"])
UTC = dt.timezone.utc
DBT_PROJECT = HERE.parents[1] / "dbt_project" / "dbt_project.yml"

CLEAN = m.Screen(assessed_days=750, ev_days=0, heater_ev_days=0,
                 heater_nights=750, heater_misread=0)
BUSY = dataclasses.replace(CLEAN, ev_days=40, heater_ev_days=40)
EV_ONLY = dataclasses.replace(CLEAN, heater_misread=100)


def clean_screens(households=COHORT) -> dict[str, m.Screen]:
    return {h["lclid"]: CLEAN for h in households}


def fake_household(i: int, solar: bool = False) -> dict:
    return {"lclid": f"MAC{i:06d}", "icp": f"09{i:08d}VE{i % 100:02d}",
            "has_solar": solar, "pv_kwp": 3.0 if solar else None,
            # solar_candidates() orders on this; descending, so a higher i
            # is tried first, as the cohort's biggest loads are.
            "nz_annual_kwh": 1000.0 * i}


#: Where each plan's plants sit in select()'s list: PLAN, SOLAR_PLAN, then the
#: demo household (cohort.json holds it).
SOLAR = slice(len(m.PLAN), len(m.PLAN) + len(m.SOLAR_PLAN))

#: PLAN's places by kind. Hot water fills first, in place order, then the EVs.
HW = [i for i, (kind, _) in enumerate(m.PLAN) if kind == "hot_water_3kw"]
EV = [i for i, (kind, _) in enumerate(m.PLAN) if kind.startswith("ev_")]


def lclids_at(plants, places):
    return [plants[i].lclid for i in places]


class TestSelection(unittest.TestCase):
    def setUp(self):
        self.plants = m.select(COHORT, START, END, clean_screens())

    def test_one_household_per_planned_load(self):
        # SOLAR_PLAN's places carry a third field, the array, so compare the
        # first two against both plans rather than the tuples whole.
        self.assertEqual([(p.kind, p.pattern) for p in self.plants],
                         [tuple(place[:2]) for place in m.PLAN + m.SOLAR_PLAN + m.DEMO_PLAN])
        self.assertEqual(len({p.icp for p in self.plants}),
                         len(m.PLAN) + len(m.SOLAR_PLAN) + len(m.DEMO_PLAN))

    def test_only_the_solar_hosts_carry_a_declared_array(self):
        # PLAN's eight have no panels at all, so an array on one of them would
        # mean gen_metering was about to net a planted charge against
        # generation that does not exist.
        for p in self.plants[:len(m.PLAN)]:
            self.assertIsNone(p.pv_kwp, p.pattern)
        for p in self.plants[SOLAR]:
            self.assertEqual(p.pv_kwp, m.SOLAR_ARRAY_KWP[p.pattern], p.pattern)

    def test_the_array_declared_is_the_one_the_scenario_needs(self):
        # The sizes are not interchangeable: only the biggest array covers
        # enough of a 3.6 kWh half-hour to push a midday run under
        # detect_high_power_excess_kwh, which is the false low_power claim
        # Stage 2 exists to remove. If midday ever stops being the largest,
        # the scenario stops being demonstrated and the stage measures
        # nothing.
        self.assertEqual(m.SOLAR_ARRAY_KWP["midday"], max(m.SOLAR_ARRAY_KWP.values()))
        self.assertGreater(m.SOLAR_ARRAY_KWP["evening"], m.SOLAR_ARRAY_KWP["timer"])

    def test_no_solar_household_takes_a_plan_place(self):
        # PLAN's eight are netted against nothing and must stay that way; the
        # solar households have their own three places below.
        solar = {h["icp"] for h in COHORT if h["has_solar"]}
        self.assertTrue(solar)
        self.assertFalse(solar & {p.icp for p in self.plants[:len(m.PLAN)]})

    def test_every_solar_plan_place_goes_to_a_household_with_panels(self):
        solar = {h["icp"] for h in COHORT if h["has_solar"]}
        picked = {p.icp for p in self.plants[SOLAR]}
        self.assertEqual(len(picked), len(m.SOLAR_PLAN))
        self.assertLessEqual(picked, solar)

    def test_the_two_pools_do_not_overlap(self):
        a = {h["lclid"] for h in m.candidates(COHORT)}
        b = {h["lclid"] for h in m.solar_candidates(COHORT)}
        self.assertTrue(a and b)
        self.assertFalse(a & b)

    def test_the_solar_pool_is_ordered_by_load_not_by_a_hash(self):
        pool = m.solar_candidates(COHORT)
        loads = [h["nz_annual_kwh"] for h in pool]
        self.assertEqual(loads, sorted(loads, reverse=True))
        self.assertEqual([p.lclid for p in self.plants[SOLAR]],
                         [h["lclid"] for h in pool[:len(m.SOLAR_PLAN)]])

    def test_the_biggest_load_gets_the_biggest_array(self):
        # Both orderings are descending -- the pool by load, SOLAR_PLAN by
        # array -- so the household that would size the largest roof is the
        # one that gets it. Stated here because nothing in the code pairs
        # them explicitly; they agree only as long as both stay sorted.
        hosts = self.plants[SOLAR]
        by_lcl = {h["lclid"]: h for h in COHORT}
        loads = [by_lcl[p.lclid]["nz_annual_kwh"] for p in hosts]
        self.assertEqual(loads, sorted(loads, reverse=True))
        self.assertEqual([p.pv_kwp for p in hosts],
                         sorted((p.pv_kwp for p in hosts), reverse=True))

    def test_the_smallest_solar_households_are_left_out(self):
        # An EV adds 2,500-4,500 kWh a year. The lightest solar households
        # would not be running one, and the ordering is what keeps them out
        # -- not a threshold anyone has to maintain.
        #
        # Stated as the invariant rather than a kWh/day constant: 05 §7 takes
        # two households out of this pool, so which household sits at the
        # bottom of it is not a fixture fact anyone should be pinning.
        pool = m.solar_candidates(COHORT)
        picked = {p.lclid for p in self.plants[SOLAR]}
        by_lcl = {h["lclid"]: h["nz_annual_kwh"] for h in pool}
        left_out = [h for h in pool if h["lclid"] not in picked]
        self.assertTrue(left_out)
        self.assertLess(max(by_lcl[h["lclid"]] for h in left_out),
                        min(by_lcl[lcl] for lcl in picked))
        # and the pool really does still contain a household too small for a car
        self.assertLess(pool[-1]["nz_annual_kwh"] / 365.25, 15.0)

    def test_nothing_about_the_solar_households_can_move_the_original_eight(self):
        # The regression this stage most has to avoid: every Sprint 4 planted
        # figure is measured against these eight, so if anything about the
        # solar households could shift one of them, the comparison is gone.
        # Reverse the solar pool's order and dirty its screens; PLAN cannot
        # notice, because it never reads a household with panels.
        moved = [dict(h, nz_annual_kwh=-h["nz_annual_kwh"]) if h["has_solar"] else h
                 for h in COHORT]
        screens = clean_screens() | {h["lclid"]: BUSY for h in COHORT
                                     if h["has_solar"]} | {
            h["lclid"]: CLEAN for h in m.solar_candidates(moved)[:len(m.SOLAR_PLAN)]}
        plants = m.select(moved, START, END, screens)
        self.assertEqual(plants[:len(m.PLAN)], self.plants[:len(m.PLAN)])
        self.assertNotEqual(plants[SOLAR], self.plants[SOLAR])

    def test_a_cohort_with_no_solar_households_fails_loudly(self):
        # Rather than quietly planting nothing: a rebuilt cohort that lost its
        # panels would make every Stage 2 measurement come back empty and
        # look like a passing detector.
        no_solar = [h for h in COHORT if not h["has_solar"]]
        with self.assertRaisesRegex(ValueError, "ev_solar_wallbox_7kw"):
            m.select(no_solar, START, END, clean_screens(no_solar))

    def test_no_defect_target_is_picked(self):
        targets = m.defect_targets(COHORT)
        self.assertEqual(len(targets), 9)
        self.assertFalse(targets & {p.icp for p in self.plants})

    def test_defect_targets_follow_the_sql(self):
        # 05 §8/§9: the first two ICPs. §7: the first two that export.
        # 07: first three and last two accounts.
        icps = sorted(h["icp"] for h in COHORT)
        accounts = sorted(COHORT, key=lambda h: m.account_number(h["lclid"]))
        targets = m.defect_targets(COHORT)
        self.assertLessEqual(set(icps[:2]), targets)
        self.assertIn(accounts[0]["icp"], targets)
        self.assertIn(accounts[-1]["icp"], targets)
        self.assertNotIn(accounts[3]["icp"], targets - set(icps[:2]))

    def test_the_first_two_exporting_icps_are_defect_targets(self):
        # 05_defects.sql §7 folds November export onto the import register
        # for these two. They are only reachable through solar_candidates(),
        # so without this exclusion a solar+EV host can land on one -- which
        # is what 99_assert.sql §15d caught on 20 Sep 2026.
        exporting = sorted(h["icp"] for h in COHORT if h["has_solar"])
        targets = m.defect_targets(COHORT)
        self.assertLessEqual(set(exporting[:2]), targets)
        self.assertFalse(set(exporting[:2]) & {p.icp for p in self.plants})

    def test_account_number_matches_gen_billing(self):
        import gen_billing
        lcl = COHORT[0]["lclid"]
        self.assertEqual(m.account_number(lcl), f"A{gen_billing._h(lcl, 'acct') % 10**8:08d}")

    def test_the_selection_is_deterministic(self):
        self.assertEqual(m.select(COHORT, START, END, clean_screens()), self.plants)
        self.assertEqual(m.select(list(reversed(COHORT)), START, END, clean_screens()),
                         self.plants)

    def test_a_limited_run_plants_the_same_households(self):
        # gen_metering selects from the whole cohort and filters afterwards.
        self.assertEqual(m.select(COHORT, START, END, clean_screens())[:2], self.plants[:2])

    def test_too_small_a_pool_fails_loudly(self):
        few = [fake_household(i) for i in range(len(m.PLAN) + 6)]
        with self.assertRaisesRegex(ValueError, "no clean candidate left"):
            m.select(few, START, END, clean_screens(few))

    def test_hot_water_takes_the_first_candidates_and_evs_the_next(self):
        pool = [h["lclid"] for h in m.candidates(COHORT)]
        self.assertEqual(lclids_at(self.plants, HW), pool[:len(HW)])
        self.assertEqual(lclids_at(self.plants, EV), pool[len(HW):len(HW) + len(EV)])

    def test_a_misread_household_is_passed_over(self):
        pool = [h["lclid"] for h in m.candidates(COHORT)]
        screens = clean_screens() | {pool[0]: BUSY}
        plants = m.select(COHORT, START, END, screens)
        self.assertNotIn(pool[0], {p.lclid for p in plants})
        self.assertEqual(lclids_at(plants, HW), pool[1:1 + len(HW)])

    def test_an_ev_only_household_is_left_for_an_ev(self):
        # Hot water is filled first, so it cannot take the household an EV
        # needs; the EV gets it although it comes first in PLAN.
        pool = [h["lclid"] for h in m.candidates(COHORT)]
        plants = m.select(COHORT, START, END, clean_screens() | {pool[0]: EV_ONLY})
        self.assertEqual(lclids_at(plants, HW), pool[1:1 + len(HW)])
        self.assertEqual(lclids_at(plants, EV), [pool[0]] + pool[1 + len(HW):len(HW) + len(EV)])

    def test_a_candidate_without_a_screen_is_never_picked(self):
        pool = [h["lclid"] for h in m.candidates(COHORT)]
        screens = clean_screens()
        del screens[pool[0]]
        self.assertNotIn(pool[0], {p.lclid for p in m.select(COHORT, START, END, screens)})

    def test_too_few_clean_households_fail_loudly(self):
        pool = [h["lclid"] for h in m.candidates(COHORT)]
        screens = {h["lclid"]: BUSY for h in COHORT}
        screens |= {lcl: CLEAN for lcl in pool[:len(m.PLAN) - 1]}
        with self.assertRaisesRegex(ValueError, "no clean candidate left for ev_portable"):
            m.select(COHORT, START, END, screens)

    def test_evs_arrive_inside_the_served_window(self):
        first, last = m.ev_window(START, END)
        self.assertGreaterEqual(first, dt.date(END.year - 1, END.month, 1))
        self.assertLessEqual(last, dt.date(END.year, END.month - 6, END.day))
        # The demo EV arrives on DEMO_EV_FROM instead (TestDemoHousehold).
        evs = [p for p in self.plants
               if p.kind.startswith("ev_") and p.lclid != m.DEMO_LCLID]
        self.assertEqual(len(evs), len(EV)
                         + sum(1 for k, *_ in m.SOLAR_PLAN if k.startswith("ev_")))
        for p in evs:
            self.assertTrue(first <= p.active_from <= last, p.active_from)
        # Each plan slices the window among its OWN EVs, so four arrivals
        # spread across it and three more spread across it again.
        for plan, plants in ((m.PLAN, self.plants[:len(m.PLAN)]),
                             (m.SOLAR_PLAN, self.plants[SOLAR])):
            n = sum(1 for place in plan if place[0].startswith("ev_"))
            arrivals = sorted(p.active_from for p in plants if p.kind.startswith("ev_"))
            # One arrival a month while there are months to go round; PLAN's
            # eight EVs share a window of about six.
            months = (last.year - first.year) * 12 + last.month - first.month + 1
            self.assertGreaterEqual(len({(a.year, a.month) for a in arrivals}), min(n, months - 1))
            width = ((last - first).days + 1) // n
            for i, a in enumerate(arrivals):
                self.assertTrue(first + dt.timedelta(days=i * width) <= a
                                < first + dt.timedelta(days=(i + 1) * width), a)

    def test_hot_water_runs_the_whole_span(self):
        for p in self.plants:
            if p.kind == "hot_water_3kw":
                self.assertEqual(p.active_from, START)


class TestSessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plants = m.select(COHORT, START, END, clean_screens())
        cls.found = {p: m.sessions(p, START, END) for p in cls.plants}

    def test_energy_per_session(self):
        for p, found in self.found.items():
            per = m.KW[p.kind] / 2
            for s in found:
                with self.subTest(kind=p.kind, start=s.start):
                    self.assertTrue(all(0 < k <= per + 1e-9 for k in s.slots))
                    self.assertTrue(all(k == per for k in s.slots[:-1]))
                    self.assertEqual(s.end - s.start, len(s.slots) * m.HALF_HOUR)
                    if s.end < self.stop:
                        self.assertGreaterEqual(s.kwh, self.minimum(p) - 1e-6)
                        self.assertLessEqual(s.kwh, self.maximum(p) + 1e-6)

    stop = dt.datetime.combine(END + dt.timedelta(days=1), dt.time(0),
                               tzinfo=tz.NZ).astimezone(UTC)

    @staticmethod
    def minimum(p):
        if p.kind == "hot_water_3kw":
            return m.HOT_WATER_KWH_BOUNDS[0]
        if p.kind == "ev_portable_2kw":
            return m.PORTABLE_MIN_SESSION_KWH
        return m.WALLBOX_KM_WEEK[0] * m.EV_KWH_PER_KM / m.SESSIONS_PER_WEEK[1]

    @staticmethod
    def maximum(p):
        if p.kind == "hot_water_3kw":
            return m.HOT_WATER_KWH_BOUNDS[1]
        km = m.WALLBOX_KM_WEEK if p.kind in m.WALLBOX_KINDS else m.PORTABLE_KM_WEEK
        return max(km[1] * m.EV_KWH_PER_KM / m.SESSIONS_PER_WEEK[0], m.PORTABLE_MIN_SESSION_KWH)

    def test_no_session_before_the_ev_arrives(self):
        for p, found in self.found.items():
            first = min(s.start.astimezone(tz.NZ).date() for s in found)
            self.assertGreaterEqual(first, p.active_from)
            self.assertLess((first - p.active_from).days, 7)

    def test_no_session_outside_the_span(self):
        begin = dt.datetime.combine(START, dt.time(0), tzinfo=tz.NZ).astimezone(UTC)
        for found in self.found.values():
            self.assertGreaterEqual(min(s.start for s in found), begin)
            self.assertLessEqual(max(s.end for s in found), self.stop)

    def test_sessions_of_one_household_never_overlap(self):
        for p, found in self.found.items():
            ordered = sorted(found, key=lambda s: s.start)
            for a, b in zip(ordered, ordered[1:]):
                self.assertLessEqual(a.end, b.start, p.kind)

    def test_ev_charges_two_to_four_times_a_week(self):
        for p, found in self.found.items():
            if not p.kind.startswith("ev_"):
                continue
            weeks: dict[dt.date, int] = {}
            for s in found:
                d = s.start.astimezone(tz.NZ).date()
                monday = d - dt.timedelta(days=d.weekday())
                weeks[monday] = weeks.get(monday, 0) + 1
            full = [n for w, n in weeks.items()
                    if w > p.active_from and w + dt.timedelta(days=6) <= END]
            self.assertTrue(full)
            self.assertTrue(all(2 <= n <= 4 for n in full), p.kind)

    def test_start_times(self):
        for p, found in self.found.items():
            clocks = {s.start.astimezone(tz.NZ).time() for s in found}
            if p.pattern == "timer":
                self.assertEqual(clocks, {dt.time(23, 0)})
            elif p.pattern == "midday":
                # No spread at all: this one charges under its own panels and
                # half an hour either way would change nothing it is here for.
                self.assertEqual(clocks, {m.MIDDAY_START})
            else:
                self.assertLessEqual(clocks, {dt.time(17, 30), dt.time(18, 0), dt.time(18, 30),
                                              dt.time(19, 0), dt.time(19, 30)})

    def test_hot_water_draws_more_in_winter(self):
        p = next(p for p in self.plants if p.kind == "hot_water_3kw")
        by_month: dict[int, list[float]] = {}
        for s in self.found[p]:
            if s.end < self.stop:
                by_month.setdefault(s.start.astimezone(tz.NZ).month, []).append(s.kwh)
        mean = {k: sum(v) / len(v) for k, v in by_month.items()}
        self.assertGreater(mean[7], mean[1] + 3)

    def test_every_night_has_its_hot_water_including_daylight_saving_days(self):
        # Sessions come from the calendar alone, so a day LCL has no reading
        # for still has one; nothing about the reads is an input.
        p = next(p for p in self.plants if p.kind == "hot_water_3kw")
        nights = {s.start.astimezone(tz.NZ).date() for s in self.found[p]}
        self.assertEqual(len(nights), (END - START).days + 1)
        odd = [d for d in nights if tz.nz_day_length_half_hours(d) != 48]
        self.assertGreaterEqual(len(odd), 4)

    def test_a_session_through_a_clock_change_keeps_its_elapsed_length(self):
        # A 23:00 session on the Saturday before daylight saving starts runs
        # through 02:00 -> 03:00. Its slots are elapsed half-hours, so it ends
        # an hour later on the clock than on an ordinary night.
        plant = m.Plant("09TEST", "MAC999999", "ev_wallbox_7kw", "timer", START)
        spring = dt.date(2026, 9, 26)          # NZ daylight saving starts 27 Sep 2026
        self.assertEqual(tz.nz_day_length_half_hours(spring + dt.timedelta(days=1)), 46)
        s = m.Session(plant.icp, plant.kind, plant.pattern,
                      m._local_start(spring, m.TIMER_START), m._slots(36.0, m.WALLBOX_KW))
        self.assertEqual(len(s.slots), 10)
        self.assertEqual(s.end.astimezone(tz.NZ).time(), dt.time(5, 0))

    def test_slot_kwh_adds_up(self):
        for p, found in self.found.items():
            slots = m.slot_kwh(found)
            self.assertAlmostEqual(sum(slots.values()), sum(s.kwh for s in found), places=6)
            self.assertLessEqual(max(slots.values()), m.KW[p.kind] / 2 + 1e-9)

    def test_slot_keys_match_the_metering_grid(self):
        # gen_metering looks planted kWh up by int(instant.timestamp()) of the
        # grid's instants; every planted slot must be one of them.
        p = next(p for p in self.plants if p.kind == "hot_water_3kw")
        fall_back = dt.date(2026, 4, 4)       # NZ daylight saving ends 5 Apr 2026
        days = [fall_back, fall_back + dt.timedelta(days=1)]
        grid = {int(i.timestamp()) for d in days for i in tz.nz_half_hour_grid(d)}
        night = [s for s in self.found[p] if s.start.astimezone(tz.NZ).date() == fall_back]
        self.assertEqual(len(night), 1)
        self.assertLessEqual(set(m.slot_kwh(night)), grid)

    def test_a_session_is_cut_at_the_end_of_the_span(self):
        p = next(p for p in self.plants if p.kind == "hot_water_3kw")
        last = max(self.found[p], key=lambda s: s.start)
        self.assertEqual(last.start.astimezone(tz.NZ).date(), END)
        self.assertEqual(last.end, self.stop)
        self.assertEqual(len(last.slots), 2)

    def test_csv_row(self):
        s = self.found[self.plants[0]][0]
        row = m.csv_row(s)
        self.assertEqual(row[:3], [s.icp, s.kind, s.pattern])
        self.assertEqual(dt.datetime.fromisoformat(row[3]), s.start)
        self.assertEqual(int(row[5]), len(s.slots))


BASE = 0.3                              # kWh a half-hour: the floor of every hand-made day
JUNE = (dt.date(2026, 6, 1), dt.date(2026, 6, 20))


def load(kwh, first=JUNE[0], last=JUNE[1]):
    """A hand-made household on the NZ grid: kwh(local instant) per half-hour."""
    days, d = [], first
    while d <= last:
        days.append((d, [(i, kwh(i.astimezone(tz.NZ))) for i in tz.nz_half_hour_grid(d)]))
        d += dt.timedelta(days=1)
    return days


def at(*clocks):
    """kwh() that draws 3 kWh at the given local times and BASE otherwise."""
    return lambda t: 3.0 if t.time() in clocks or (t.date(), t.time()) in clocks else BASE


class TestScreen(unittest.TestCase):
    def screen(self, kwh, first=JUNE[0], last=JUNE[1]):
        return m.screen("MAC000001", load(kwh, first, last), first, last)

    def test_the_thresholds_are_detect_v1s(self):
        if not DBT_PROJECT.exists():
            self.skipTest("dbt project not in this checkout")
        dbt = dict(re.findall(r"^\s*(detect_\w+):\s*([0-9.]+)\s*$",
                              DBT_PROJECT.read_text(), re.M))
        for name, value in m.DETECT_V1.items():
            self.assertEqual(float(dbt[name]), value, name)

    def test_a_flat_load_is_clean(self):
        sc = self.screen(lambda t: BASE)
        self.assertEqual(sc, m.Screen(20, 0, 0, 20, 0))
        self.assertTrue(sc.fits("ev_wallbox_7kw"))
        self.assertTrue(sc.fits("hot_water_3kw"))

    def test_an_hour_at_five_kw_is_ev_shaped(self):
        sc = self.screen(at(dt.time(20), dt.time(20, 30)))
        self.assertEqual((sc.ev_days, sc.heater_ev_days, sc.heater_misread), (20, 20, 0))
        self.assertFalse(sc.fits("ev_wallbox_7kw"))
        self.assertFalse(sc.fits("hot_water_3kw"))

    def test_three_steady_hours_at_two_kw_are_ev_shaped(self):
        sc = self.screen(lambda t: 1.5 if 13 <= t.hour < 16 else BASE)
        self.assertEqual(sc.ev_days, 20)

    def test_a_spike_and_a_short_mid_run_are_not(self):
        sc = self.screen(lambda t: 3.0 if t.time() == dt.time(8)
                         else 1.5 if t.hour == 13 else BASE)
        self.assertEqual(sc.ev_days, 0)

    def test_a_run_counts_on_each_date_it_touches(self):
        sc = self.screen(at((dt.date(2026, 6, 5), dt.time(23, 30)),
                            (dt.date(2026, 6, 6), dt.time(0))))
        self.assertEqual(sc.ev_days, 2)

    def test_a_run_crosses_a_clock_change(self):
        # NZ daylight saving starts 27 Sep 2026: 01:30 is followed by 03:00.
        day = dt.date(2026, 9, 27)
        sc = self.screen(at(dt.time(1, 30), dt.time(3)), day, day)
        self.assertEqual(sc.ev_days, 1)

    def test_a_missing_read_ends_a_run_and_the_day_is_not_assessed(self):
        gap = dt.date(2026, 6, 5)
        high = at(dt.time(20), dt.time(20, 30), dt.time(21))

        def kwh(t):
            return None if (t.date(), t.time()) == (gap, dt.time(20, 30)) else high(t)

        assessed, runs = m._runs(load(kwh), {})
        self.assertEqual(len(assessed), 19)
        self.assertNotIn(gap, assessed)
        self.assertEqual(len(runs), 19)
        self.assertFalse(any(d == gap for r in runs for _, d, _ in r))

    def test_no_assessed_day_fits_nothing(self):
        sc = m.Screen(0, 0, 0, 0, 0)
        self.assertFalse(sc.fits("ev_wallbox_7kw"))
        self.assertFalse(sc.fits("hot_water_3kw"))

    def test_the_limits_are_inclusive(self):
        n = 1000
        edge = m.Screen(n, round(n * m.MAX_EV_SHAPED_SHARE), round(n * m.MAX_EV_SHAPED_SHARE),
                        n, round(n * m.MAX_TIMER_MISREAD_SHARE))
        self.assertTrue(edge.fits("ev_portable_2kw"))
        self.assertTrue(edge.fits("hot_water_3kw"))
        self.assertFalse(dataclasses.replace(edge, ev_days=edge.ev_days + 1).fits("ev_wallbox_7kw"))
        self.assertFalse(dataclasses.replace(edge, heater_ev_days=edge.ev_days + 1)
                         .fits("hot_water_3kw"))
        self.assertFalse(dataclasses.replace(edge, heater_misread=edge.heater_misread + 1)
                         .fits("hot_water_3kw"))

    def test_the_solar_bound_is_the_looser_one(self):
        # Stated as an inequality rather than as two numbers, so tightening
        # the solar bound past PLAN's -- which would silently make the solar
        # pool the stricter of the two and empty it -- fails here.
        self.assertGreater(m.MAX_SOLAR_EV_SHAPED_SHARE, m.MAX_EV_SHAPED_SHARE)

    def test_only_the_solar_kind_gets_the_looser_bound(self):
        # The whole point of a separate bound: a household too EV-shaped for
        # PLAN can still host a solar+EV wallbox, and nothing about PLAN's
        # eight moves. A single shared bound would have done one or the other.
        n = 1000
        over = round(n * m.MAX_EV_SHAPED_SHARE) + 1
        between = m.Screen(n, over, over, n, 0)
        self.assertTrue(between.fits("ev_solar_wallbox_7kw"))
        for kind in ("ev_wallbox_7kw", "ev_portable_2kw", "hot_water_3kw"):
            self.assertFalse(between.fits(kind), kind)

    def test_the_solar_bound_is_a_bound_and_not_a_waiver(self):
        n = 1000
        over = m.Screen(n, round(n * m.MAX_SOLAR_EV_SHAPED_SHARE) + 1, 0, n, 0)
        self.assertFalse(over.fits("ev_solar_wallbox_7kw"))

    def test_a_load_an_hour_before_the_timer_moves_its_start(self):
        # 22:00-23:00 joins the element's run, which then starts in the wrong slot.
        sc = self.screen(lambda t: 1.2 if t.hour == 22 else BASE)
        self.assertEqual((sc.ev_days, sc.heater_misread), (0, 20))
        self.assertTrue(sc.fits("ev_wallbox_7kw"))
        self.assertFalse(sc.fits("hot_water_3kw"))

    def test_a_load_half_an_hour_before_is_inside_the_tolerance(self):
        sc = self.screen(lambda t: 1.2 if t.time() == dt.time(22, 30) else BASE)
        self.assertEqual(sc.heater_misread, 0)

    def test_a_load_under_the_timer_makes_it_high_power(self):
        # Alone, 1.1 kWh above the floor for an hour is nothing; with the
        # element's 1.5 on top it is a 5 kW run on every night.
        sc = self.screen(lambda t: 1.4 if t.hour == 23 else BASE)
        self.assertEqual((sc.ev_days, sc.heater_misread, sc.heater_ev_days), (0, 20, 20))
        self.assertTrue(sc.fits("ev_wallbox_7kw"))
        self.assertFalse(sc.fits("hot_water_3kw"))



class TestDemoHousehold(unittest.TestCase):
    """planted.DEMO_LCLID: one account that walks through every feature."""

    def test_the_demo_household_is_in_the_cohort_without_panels(self):
        demo = m.demo_candidates(COHORT)
        self.assertEqual([h["lclid"] for h in demo], [m.DEMO_LCLID])

    def test_it_is_in_no_other_pool(self):
        pools = m.candidates(COHORT) + m.solar_candidates(COHORT)
        self.assertNotIn(m.DEMO_LCLID, {h["lclid"] for h in pools})

    def test_it_gets_one_evening_wallbox_and_moves_no_other_plant(self):
        with_demo = m.select(COHORT, START, END, clean_screens())
        without = m.select([h for h in COHORT if h["lclid"] != m.DEMO_LCLID],
                           START, END, clean_screens())
        self.assertEqual(with_demo[:len(without)], without)
        demo = with_demo[len(without):]
        self.assertEqual([(p.lclid, p.kind, p.pattern) for p in demo],
                         [(m.DEMO_LCLID, "ev_wallbox_7kw", "evening")])
        # Before the weather record ends, so the bill it raises is explainable.
        self.assertEqual(demo[0].active_from, max(START, m.DEMO_EV_FROM))

if __name__ == "__main__":
    unittest.main()
