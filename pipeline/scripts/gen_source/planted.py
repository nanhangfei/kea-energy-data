"""Loads planted on top of the LCL households, so detection has known answers.

The Low Carbon London trial (2011-14) has no electric vehicles, and nothing in
it is labelled, so a detector run on it can say what it found but never
whether it was right. This module chooses a few households and gives them a
load whose every session is known: three 7 kW wallboxes, one 2 kW portable
charger, four hot-water elements on a 23:00 timer, and three more wallboxes
on households that also have panels. gen_metering.py adds
the kWh to the household's load and writes the sessions to
planted_loads.csv.gz, which load_source.sh copies into gen_audit.planted_load.
The answers stay in gen_audit and never reach serving (design §15).

Pure and deterministic: it reads nothing but cohort.json and the load
gen_metering.py hands screen(), and every other choice is a hash of the
household's LCL id, as has_solar is in cohort.py. No `random`.

Four rules keep the labels clean:

  * SOLAR IS A SEPARATE POOL, NEVER A SILENT ONE. A planted kWh on a solar
    household is partly netted against generation and never shows on the
    import register in full, so PLAN's eight places take only households
    without panels and always will. SOLAR_PLAN's three places do the
    opposite on purpose: they are the only households in the cohort with
    both panels and an EV, and they exist so the detector can be measured
    where the meter is blind rather than quietly degraded there. The two
    pools never overlap and no household is in both.

  * NO DEFECT TARGET. The households the source defects already break are
    left out: the two ICPs 05_defects.sql §8 and §9 pick (the first two cohort
    ICPs in order) and the five accounts 07_billing_defects.sql orphans (the
    first three and the last two by account number). 99_assert.sql checks it.

  * SESSIONS DO NOT DEPEND ON THE READS. A session is scheduled on the
    calendar, so a daylight-saving day or a day LCL has no reading for still
    has its sessions. A half-hour with no reading gets no row, planted or not,
    and the evaluation leaves days that were not read in full out.

  * A CLEAN OWN LOAD. LCL households have big loads of their own, and scaled
    x1.72 a UK heater draws like a charger: detect-v1 finds EV-shaped runs in
    27 of the 50. A place goes only to a household the detector would almost
    never misread (screen()). For an EV, its own load has an EV-shaped run on
    at most MAX_EV_SHAPED_SHARE of its assessed days -- or, for SOLAR_PLAN's
    three, MAX_SOLAR_EV_SHAPED_SHARE, because only eight households have
    panels and the strict bound leaves two of them for three places. For a
    hot-water element,
    the same holds with the element added, and the element reads as a timer
    on all but MAX_TIMER_MISREAD_SHARE of its nights. gen_metering.py screens
    every candidate before it plants anything. It is the one choice that reads
    the LCL load, and it only ever leaves a household out.

The EV arrives mid-span (`active_from`), inside the 13 months serving keeps and
at least six months before the span ends, so the bill driver and the heating
fit see a real "new appliance" step.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import math
import statistics
from dataclasses import dataclass

NZ_TZ = "Pacific/Auckland"
HALF_HOUR = dt.timedelta(minutes=30)

#: kW at the plug, and the kWh one half-hour of it draws.
WALLBOX_KW = 7.2
PORTABLE_KW = 2.3
HOT_WATER_KW = 3.0

#: kWh per km at the plug, charging losses included. [ASSUMED]
EV_KWH_PER_KM = 0.18
#: Weekly km, drawn per household-week. [ASSUMED]
WALLBOX_KM_WEEK = (250.0, 500.0)
PORTABLE_KM_WEEK = (150.0, 300.0)
#: A portable session shorter than this would not look like charging at all.
PORTABLE_MIN_SESSION_KWH = 8.0
#: Sessions per week, inclusive.
SESSIONS_PER_WEEK = (2, 4)
#: Evening plug-in times, as half-hour offsets from 17:30.
EVENING_FIRST = dt.time(17, 30)
EVENING_SLOTS = 5                      # 17:30 .. 19:30
TIMER_START = dt.time(23, 0)
#: Plugged in late morning, so the session runs through the middle of the day.
#: Only a solar household charges this way here -- it is the case where the
#: panels hide the charge outright rather than merely shaving it.
MIDDAY_START = dt.time(11, 0)

#: A hot-water cylinder on a timer, kWh a night: more in winter.
HOT_WATER_KWH_MEAN = 6.5
HOT_WATER_KWH_SWING = 2.5
HOT_WATER_KWH_BOUNDS = (4.0, 9.0)
#: Mid-July, the middle of the NZ winter.
HOT_WATER_PEAK_DOY = 196

#: The EV arrives at least this long before the span ends ...
EV_MIN_MONTHS_BEFORE_END = 6
#: ... and after the first month of the 13 serving keeps, so a whole bill
#: before its arrival is served too.
SERVED_MONTHS = 13
EV_MARGIN_AFTER_SERVED_START = dt.timedelta(days=35)

#: (kind, pattern) for each planted household WITHOUT PANELS, in selection
#: order. A solar household never joins this one: see SOLAR_PLAN.
PLAN = (
    ("ev_wallbox_7kw", "evening"),
    ("ev_wallbox_7kw", "evening"),
    ("ev_wallbox_7kw", "timer"),
    ("ev_portable_2kw", "evening"),
    ("hot_water_3kw", "timer"),
    ("hot_water_3kw", "timer"),
    ("hot_water_3kw", "timer"),
    ("hot_water_3kw", "timer"),
)

#: (kind, pattern, kWp) for each planted household WITH PANELS, in selection
#: order, which solar_candidates() makes biggest load first. The three exist
#: to demonstrate three different things, and this order is what assigns them:
#:
#:   midday   the worst case, and the reason detect-v2 exists. A 7.2 kW
#:            wallbox draws 3.6 kWh a half-hour; at summer solar noon an
#:            8 kWp array covers 2.19 of it, so the meter reads about 1.8 and
#:            the run is confidently called low_power -- the exact false
#:            claim this work removes. In winter the same array covers 1.34,
#:            the run clears 2.5 again, and high_power is right once more.
#:   evening  partial masking, and it moves with the season: the same
#:            appliance at the same clock time is masked on a December
#:            evening and not on a June one, so the household changes class
#:            with nothing in the house changing.
#:   timer    the control. At 23:00 the panels generate nothing, so this
#:            household must be detected exactly as a non-solar one is. If it
#:            is not, detect-v2 is damaging the night and is wrong.
#:
#: The kWp is DECLARED here rather than taken from cohort.json, which sizes
#: an array to the household's load BEFORE it had a car. A household that
#: buys an EV and charges it off its own roof sizes the roof for the car:
#: 8 kWp yields 10,400 kWh a year against this host's ~9,700 kWh of load
#: with the EV. cohort.py cannot compute that -- it does not know which
#: households will be planted -- so gen_metering resolves the two, and this
#: is the side that wins.
SOLAR_PLAN = (
    ("ev_solar_wallbox_7kw", "midday", 8.0),
    ("ev_solar_wallbox_7kw", "evening", 5.0),
    ("ev_solar_wallbox_7kw", "timer", 3.0),
)

#: The array on a solar+EV host's roof, by the pattern it charges on. The
#: pattern reaches gen_audit.planted_load, so the evaluation can recover the
#: array from the database without re-running the selection.
SOLAR_ARRAY_KWP = {pattern: kwp for _, pattern, kwp in SOLAR_PLAN}

#: The kinds that draw at WALLBOX_KW, whatever the roof is doing.
WALLBOX_KINDS = ("ev_wallbox_7kw", "ev_solar_wallbox_7kw")

KW = {"ev_wallbox_7kw": WALLBOX_KW, "ev_solar_wallbox_7kw": WALLBOX_KW,
      "ev_portable_2kw": PORTABLE_KW, "hot_water_3kw": HOT_WATER_KW}

#: detect-v1's thresholds (dbt_project.yml), which screen() applies to a
#: household's own load. A test fails if the two drift apart.
DETECT_V1 = {
    "detect_min_excess_kwh": 0.75,
    "detect_min_run_half_hours": 2,
    "detect_high_power_excess_kwh": 2.5,
    "detect_high_power_min_half_hours": 2,
    "detect_slot_tolerance": 1,
    "detect_timed_min_excess_kwh": 0.75,
    "detect_timed_max_excess_kwh": 2.0,
    "detect_low_power_min_half_hours": 6,
    "detect_low_power_min_excess_kwh": 0.9,
    "detect_low_power_max_excess_kwh": 2.0,
    "detect_low_power_max_excess_sd": 0.35,
}
#: A place goes to a household whose own load has an EV-shaped run on at most
#: this share of its assessed days: 3 of the span's 757. Stage 4 counts every
#: such run in a planted household as a false session. [ASSUMED]
MAX_EV_SHAPED_SHARE = 0.005
#: SOLAR_PLAN's bound, deliberately looser: 11 of the span's 757 days.
#: Only 8 households in the cohort have panels, and about a quarter of any
#: sample of LCL households has an own load this clean -- measured, the solar
#: eight score 0, 0, 4, 9, 44, 102, 105 and 546 EV-shaped days. At 0.005
#: there are two eligible hosts and three places, and the two are the
#: lightest households in the cohort, which would not be running a car. So
#: the solar pool trades a little label noise for hosts that exist: Stage 4
#: reports each host's own-load background beside its recall rather than
#: pretending it is zero. PLAN's eight keep the strict bound, untouched, so
#: no Sprint 4 figure moves. [ASSUMED]
MAX_SOLAR_EV_SHAPED_SHARE = 0.015
#: A hot-water place also needs the element read as a timer on all but this
#: share of its nights; Stage 4's target is 80% detected. [ASSUMED]
MAX_TIMER_MISREAD_SHARE = 0.02


def _h(*parts) -> int:
    """The generators' hash: gen_metering, gen_reference and gen_billing use it."""
    return int(hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12], 16)


def _u(seed_parts, lo: float, hi: float) -> float:
    return lo + (_h(*seed_parts) % 10**6) / 10**6 * (hi - lo)


def _zone():
    from zoneinfo import ZoneInfo
    return ZoneInfo(NZ_TZ)


# ------------------------------------------------------------ who is picked

def account_number(lclid: str) -> str:
    """Must match gen_billing.py's acct_no exactly."""
    return f"A{_h(lclid, 'acct') % 10**8:08d}"


def defect_targets(households: list[dict]) -> set[str]:
    """ICPs a source defect already breaks, computed as the SQL picks them.

    05_defects.sql §8 takes the first generated ICP, and §9 the second (it
    skips the first, which §8 made ambiguous). §7 takes the first two ICPs
    that have an export register and folds their November 2025 export onto
    the import one. 07_billing_defects.sql orphans the first three accounts
    by account number and the last two. The cohort's ICPs and account numbers
    are fixed-width digits and capitals, so Python's ordering is Postgres's.

    ⚠️ §7 MATTERS ONLY TO THE SOLAR POOL, which is why it was missing here
    until 20 Sep 2026: it can only ever land on a generating household, and
    candidates() takes none. solar_candidates() takes nothing else, so for
    that pool §7 is the defect most likely to bite -- a host whose November
    import carries its own export is not a household the detector can be
    measured on. 99_assert.sql §15d counts all five defect kinds and fails
    the load if a planted load lands on any of them.
    """
    icps = sorted(h["icp"] for h in households)
    exporting = sorted(h["icp"] for h in households if h["has_solar"])
    by_account = sorted(households, key=lambda h: account_number(h["lclid"]))
    orphans = by_account[:3] + by_account[-2:]
    return set(icps[:2]) | set(exporting[:2]) | {h["icp"] for h in orphans}


@dataclass(frozen=True)
class Plant:
    icp: str
    lclid: str
    kind: str
    pattern: str                  # 'evening', 'timer' or 'midday'
    active_from: dt.date
    #: The array SOLAR_PLAN declares for this host, None for PLAN's eight.
    #: gen_metering prefers it over the cohort row's pv_kwp; see SOLAR_PLAN.
    pv_kwp: float | None = None


def _months_before(day: dt.date, months: int) -> dt.date:
    y, m = divmod(day.year * 12 + day.month - 1 - months, 12)
    m += 1
    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def ev_window(nz_start: dt.date, nz_end: dt.date) -> tuple[dt.date, dt.date]:
    """The dates an EV may arrive on, both inclusive."""
    first = max(nz_start, _months_before(nz_end, SERVED_MONTHS)) + EV_MARGIN_AFTER_SERVED_START
    last = _months_before(nz_end, EV_MIN_MONTHS_BEFORE_END)
    if first > last:
        raise ValueError(f"span {nz_start}..{nz_end} leaves no room for an EV to arrive")
    return first, last


def candidates(households: list[dict]) -> list[dict]:
    """The households a PLAN load may go to, in the order they are tried."""
    excluded = defect_targets(households)
    return sorted((h for h in households
                   if not h["has_solar"] and h["icp"] not in excluded),
                  key=lambda h: (_h(h["lclid"], "planted"), h["lclid"]))


def solar_candidates(households: list[dict]) -> list[dict]:
    """The households a SOLAR_PLAN load may go to, biggest NZ load first.

    This is the one order in this module that is not a hash, and deliberately:
    panels and an EV go together at the top of the load distribution, not the
    bottom. An EV adds 2,500-4,500 kWh a year, and the cohort's smallest solar
    households use 6.6 and 10.2 kWh a day scaled -- giving one of those a
    wallbox would plant a household that does not exist. Ordering by load puts
    the three places on the three households that would plausibly have one and
    leaves the rest as fallbacks if a screen turns one down.

    Same defect-target exclusion as candidates(), and no overlap with it: this
    pool is exactly the households that one filters out.
    """
    excluded = defect_targets(households)
    return sorted((h for h in households
                   if h["has_solar"] and h["icp"] not in excluded),
                  key=lambda h: (-h["nz_annual_kwh"], h["lclid"]))


def _fill(plan, pool: list[dict], screens: dict[str, Screen]) -> dict[int, dict]:
    """A household per place of `plan`, taken from `pool` in its own order.

    Each place takes the first candidate not yet planted whose screen fits it;
    `screens` is keyed by LCL id, and a candidate without one is never picked.
    The hot-water places are filled first: their screen holds the EV one and
    more, so an EV never takes a household only an element could have had.
    """
    chosen: dict[int, dict] = {}
    for i in sorted(range(len(plan)), key=lambda i: plan[i][0].startswith("ev_")):
        kind, pattern = plan[i][0], plan[i][1]
        taken = {h["lclid"] for h in chosen.values()}
        h = next((h for h in pool if h["lclid"] not in taken
                  and h["lclid"] in screens and screens[h["lclid"]].fits(kind)), None)
        if h is None:
            raise ValueError(f"no clean candidate left for {kind}/{pattern}: "
                             f"{len(pool)} candidates, {len(chosen)} already planted")
        chosen[i] = h
    return chosen


def _plants(plan, chosen: dict[int, dict],
            nz_start: dt.date, nz_end: dt.date) -> list[Plant]:
    """`plan`'s places as Plants, each EV arriving in its own slice of the
    window so the arrivals spread across it rather than wherever the hashes
    happen to cluster. The slicing is per plan: SOLAR_PLAN's three EVs share
    the whole window, exactly as PLAN's four do."""
    first, last = ev_window(nz_start, nz_end)
    n_ev = sum(1 for place in plan if place[0].startswith("ev_"))
    width = ((last - first).days + 1) // max(n_ev, 1)
    out: list[Plant] = []
    ev_seen = 0
    for i, place in enumerate(plan):
        kind, pattern = place[0], place[1]
        kwp = place[2] if len(place) > 2 else None
        h = chosen[i]
        if kind.startswith("ev_"):
            active = first + dt.timedelta(
                days=ev_seen * width + _h(h["lclid"], "ev_from") % width)
            ev_seen += 1
        else:
            active = nz_start
        out.append(Plant(h["icp"], h["lclid"], kind, pattern, active, kwp))
    return out


def select(households: list[dict], nz_start: dt.date, nz_end: dt.date,
           screens: dict[str, Screen]) -> list[Plant]:
    """The planted households: PLAN's eight, then SOLAR_PLAN's three.

    The two plans are filled from their own pools, in their own order, and
    neither can reach into the other's -- so adding SOLAR_PLAN cannot move a
    single one of PLAN's eight. Always chosen from the WHOLE cohort, so a
    `--limit` run plants the same households as a full one.
    """
    return (_plants(PLAN, _fill(PLAN, candidates(households), screens),
                    nz_start, nz_end)
            + _plants(SOLAR_PLAN,
                      _fill(SOLAR_PLAN, solar_candidates(households), screens),
                      nz_start, nz_end))


# ------------------------------------------------------------ the sessions

@dataclass(frozen=True)
class Session:
    icp: str
    kind: str
    pattern: str
    start: dt.datetime            # aware, UTC
    slots: tuple[float, ...]      # kWh per half-hour from `start`

    @property
    def end(self) -> dt.datetime:
        return self.start + len(self.slots) * HALF_HOUR

    @property
    def kwh(self) -> float:
        return sum(self.slots)


def _slots(kwh: float, kw: float) -> tuple[float, ...]:
    """Full half-hours at `kw`, then the remainder in the last one."""
    per = kw / 2
    full = int(kwh // per)
    rest = round(kwh - full * per, 6)
    return (per,) * full + ((rest,) if rest > 0 else ())


def _local_start(day: dt.date, clock: dt.time) -> dt.datetime:
    """A local wall clock as an absolute instant.

    Every clock time used here (11:00, 17:30-19:30, 23:00) exists on every
    Auckland day: the clocks change at 02:00 and 03:00. Everything after this is UTC arithmetic, so a session running
    through a change is as long in elapsed time as on any other night.
    """
    return dt.datetime.combine(day, clock, tzinfo=_zone()).astimezone(dt.timezone.utc)


def hot_water_kwh(plant: Plant, day: dt.date) -> float:
    doy = day.timetuple().tm_yday
    season = math.cos(2 * math.pi * (doy - HOT_WATER_PEAK_DOY) / 365.25)
    kwh = (HOT_WATER_KWH_MEAN + HOT_WATER_KWH_SWING * season
           + _u((plant.lclid, day, "hw"), -0.3, 0.3))
    lo, hi = HOT_WATER_KWH_BOUNDS
    return round(min(hi, max(lo, kwh)), 4)


def _ev_days(plant: Plant, monday: dt.date) -> list[int]:
    lo, hi = SESSIONS_PER_WEEK
    n = lo + _h(plant.lclid, monday, "n") % (hi - lo + 1)
    return sorted(sorted(range(7), key=lambda d: _h(plant.lclid, monday, d, "day"))[:n])


def _ev_session_kwh(plant: Plant, monday: dt.date, n: int) -> float:
    km = _u((plant.lclid, monday, "km"),
            *(WALLBOX_KM_WEEK if plant.kind in WALLBOX_KINDS else PORTABLE_KM_WEEK))
    kwh = km * EV_KWH_PER_KM / n
    if plant.kind == "ev_portable_2kw":
        kwh = max(kwh, PORTABLE_MIN_SESSION_KWH)
    return round(kwh, 4)


def sessions(plant: Plant, nz_start: dt.date, nz_end: dt.date) -> list[Session]:
    """Every session of one planted household inside the span.

    A session still running when the span ends is cut there, as the meter
    would be, and its recorded kWh is what was drawn inside the span.
    """
    stop = dt.datetime.combine(nz_end + dt.timedelta(days=1), dt.time(0),
                               tzinfo=_zone()).astimezone(dt.timezone.utc)
    kw = KW[plant.kind]
    out: list[Session] = []

    def add(start: dt.datetime, kwh: float) -> None:
        slots = _slots(kwh, kw)
        room = int((stop - start) / HALF_HOUR)
        slots = slots[:max(room, 0)]
        if slots:
            out.append(Session(plant.icp, plant.kind, plant.pattern, start, slots))

    if plant.kind == "hot_water_3kw":
        day = max(plant.active_from, nz_start)
        while day <= nz_end:
            add(_local_start(day, TIMER_START), hot_water_kwh(plant, day))
            day += dt.timedelta(days=1)
        return out

    base = _h(plant.lclid, "evening") % EVENING_SLOTS
    monday = plant.active_from - dt.timedelta(days=plant.active_from.weekday())
    while monday <= nz_end:
        days = _ev_days(plant, monday)
        kwh = _ev_session_kwh(plant, monday, len(days))
        for d in days:
            day = monday + dt.timedelta(days=d)
            if day < max(plant.active_from, nz_start) or day > nz_end:
                continue
            if plant.pattern == "timer":
                clock = TIMER_START
            elif plant.pattern == "midday":
                # No spread: the point of this household is that it charges
                # while the panels generate, and half an hour either way
                # would not change what the meter can see.
                clock = MIDDAY_START
            else:
                # Plugged in at the household's usual time, give or take half an hour.
                offset = base + _h(plant.lclid, day, "late") % 3 - 1
                offset = min(max(offset, 0), EVENING_SLOTS - 1)
                minutes = EVENING_FIRST.hour * 60 + EVENING_FIRST.minute + 30 * offset
                clock = dt.time(minutes // 60, minutes % 60)
            add(_local_start(day, clock), kwh)
        monday += dt.timedelta(days=7)
    return out


def slot_kwh(planted: list[Session]) -> dict[int, float]:
    """Planted kWh by half-hour, keyed on the instant's POSIX timestamp.

    Keyed on the timestamp rather than the datetime: an Auckland wall clock
    that occurs twice on the fall-back day differs only in `fold`, and an
    integer cannot be confused that way.
    """
    out: dict[int, float] = {}
    for s in planted:
        for i, kwh in enumerate(s.slots):
            key = int((s.start + i * HALF_HOUR).timestamp())
            out[key] = out.get(key, 0.0) + kwh
    return out


def csv_row(s: Session) -> list[str]:
    return [s.icp, s.kind, s.pattern, s.start.isoformat(), s.end.isoformat(),
            str(len(s.slots)), f"{s.kwh:.4f}"]


# ------------------------------------------------------------ the screen

@dataclass(frozen=True)
class Screen:
    """What detect-v1 would read in one household's load, before planting."""
    assessed_days: int
    ev_days: int            # assessed days with an EV-shaped run in its own load
    heater_ev_days: int     # the same with a 23:00 element added, its runs aside
    heater_nights: int      # assessed nights the element runs
    heater_misread: int     # of those, nights its run would not read as a timer

    def fits(self, kind: str) -> bool:
        if not self.assessed_days:
            return False
        share = (MAX_SOLAR_EV_SHAPED_SHARE if kind == "ev_solar_wallbox_7kw"
                 else MAX_EV_SHAPED_SHARE)
        limit = share * self.assessed_days
        if kind.startswith("ev_"):
            return self.ev_days <= limit
        return (self.heater_ev_days <= limit
                and self.heater_misread <= MAX_TIMER_MISREAD_SHARE * self.heater_nights)


#: One half-hour of a run: POSIX timestamp, NZ date, kWh above that date's floor.
_Point = tuple[int, dt.date, float]
_STEP = int(HALF_HOUR.total_seconds())


def _runs(days, added: dict[int, float]) -> tuple[set[dt.date], list[list[_Point]]]:
    """detect-v1's runs in a household's load, and the days it assesses.

    As int_metering__import_half_hours: the floor is the day's smallest
    half-hour, a candidate is detect_min_excess_kwh above its own date's
    floor, and a run is consecutive candidates on the continuous timeline, so
    it crosses midnight and a clock change and a missing read ends it. Tails
    are left out; they never change a run's class.
    """
    assessed: set[dt.date] = set()
    points: list[_Point] = []
    for day, reads in days:
        stamped = [(int(i.timestamp()), k) for i, k in reads]
        kwh = [(t, k + added.get(t, 0.0)) for t, k in stamped if k is not None]
        if not kwh:
            continue
        if len(kwh) == len(reads):
            assessed.add(day)
        floor = min(k for _, k in kwh)
        points += [(t, day, k - floor) for t, k in kwh]
    runs: list[list[_Point]] = []
    run: list[_Point] = []
    for p in points + [(0, dt.date.min, -1.0)]:
        candidate = p[2] >= DETECT_V1["detect_min_excess_kwh"]
        if candidate and run and p[0] - run[-1][0] == _STEP:
            run.append(p)
            continue
        if len(run) >= DETECT_V1["detect_min_run_half_hours"]:
            runs.append(run)
        run = [p] if candidate else []
    return assessed, runs


def _high_power(run: list[_Point]) -> bool:
    """detect-v1 rule 2."""
    v = DETECT_V1
    high = sum(1 for *_, e in run if e >= v["detect_high_power_excess_kwh"])
    return high >= v["detect_high_power_min_half_hours"]


def _ev_shaped(run: list[_Point]) -> bool:
    """detect-v1 rule 2, or rule 4's shape. Rules 1 and 3 are not applied, so a
    run they would claim counts too: the screen errs towards leaving out."""
    v = DETECT_V1
    excess = [e for *_, e in run]
    return _high_power(run) or (
        len(excess) >= v["detect_low_power_min_half_hours"]
        and v["detect_low_power_min_excess_kwh"] <= statistics.fmean(excess)
        <= v["detect_low_power_max_excess_kwh"]
        and statistics.pstdev(excess) <= v["detect_low_power_max_excess_sd"])


def _reads_as_timer(run: list[_Point], start: int) -> bool:
    """detect-v1 rule 3 for an element switched on at `start`: the run starts
    within detect_slot_tolerance before it, is not high-power, and draws in
    the timed band. The run's own start slot is what rule 3 compares."""
    v = DETECT_V1
    return (run[0][0] >= start - v["detect_slot_tolerance"] * _STEP
            and not _high_power(run)
            and v["detect_timed_min_excess_kwh"] <= statistics.fmean(e for *_, e in run)
            <= v["detect_timed_max_excess_kwh"])


def screen(lclid: str, days, nz_start: dt.date, nz_end: dt.date) -> Screen:
    """detect-v1 on one household's load, alone and with a hot-water element.

    `days` is the scaled load on the NZ grid, as gen_metering builds it:
    [(nz_date, [(instant, kWh or None), ...])] in date order.
    """
    def dates(runs) -> set[dt.date]:
        return {d for r in runs for _, d, _ in r}

    assessed, own = _runs(days, {})
    element = Plant("", lclid, "hot_water_3kw", "timer", nz_start)
    nights = sessions(element, nz_start, nz_end)
    _, heated = _runs(days, slot_kwh(nights))
    run_of = {t: n for n, r in enumerate(heated) for t, _, _ in r}
    timer: set[int] = set()
    counted = misread = 0
    for s in nights:
        if s.start.astimezone(_zone()).date() not in assessed:
            continue
        counted += 1
        start = int(s.start.timestamp())
        n = run_of.get(start)
        if n is not None and _reads_as_timer(heated[n], start):
            timer.add(n)
        else:
            misread += 1
    return Screen(
        assessed_days=len(assessed),
        ev_days=len(dates(r for r in own if _ev_shaped(r)) & assessed),
        heater_ev_days=len(dates(r for n, r in enumerate(heated)
                                 if n not in timer and _ev_shaped(r)) & assessed),
        heater_nights=counted,
        heater_misread=misread)
