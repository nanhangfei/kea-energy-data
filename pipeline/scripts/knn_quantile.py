"""kNN quantile forecasts for one household (Botman et al. 2025).

    L. Botman et al., "A global probabilistic approach for short-term
    forecasting of individual households electricity consumption",
    Applied Energy 382 (2025) 125168, sections 2.2.5, 2.3 and 3.

The model and its scores only: no I/O, no database, no lake.
backtest_bill_forecast.py reads the export and calls these.

THE METHOD, IN ONE PARAGRAPH. Take a household's seven days before the target
day and divide them by that week's largest half-hour ("week normalisation").
Compare them, by Euclidean distance, with every seven-day window of every
household that is already history, each divided by its own maximum. The k
nearest windows each bring the day that followed them, still on their own
scale; multiplied by the query week's maximum, those k days are the forecast,
an equally weighted ensemble of k values per half-hour.

MONTH-END (STAGE 2) extends the paper: each neighbour carries its whole
continuation to the end of the billing cycle, summed before any quantile is
taken. trajectory_library() and remaining_ensemble() below.

STAGE 3 ADDS TWO DIALS, both off by default, so the headline stays the
paper's. `norm` picks what a window is divided by before the comparison --
the paper's week maximum, the week's mean ("profile", shape only), or
nothing at all. seasonal_rows() restricts the library to the same time of
year a year earlier, this backtest's stand-in for weather, since the paper
uses none.

THE LEAKAGE RULE IS STRICTER THAN THE PAPER'S WORDING. Section 3.3 builds
windows "up to and excluding day d", which lets another household's day d be a
neighbour's next day -- a value nobody would have at forecast time. Here a
window is usable for day d only when the window AND its continuation end before
d, for every household, the target's own included. Library.last_day carries
that date and neighbours() enforces it; test_knn_quantile.py pins it.

WHY NUMPY IS IMPORTED INSIDE EACH FUNCTION. The host test step (`make ci
ONLY=test`) is standard-library only. Importing this module there must work, so its tests can
skip cleanly; only calling it needs numpy, which the dataeng/dbt image has.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

NZ = "Pacific/Auckland"
SLOTS = 48                 # half-hours in a local clock day
WINDOW_DAYS = 7            # the paper's query length
PAPER_K = 100              # the paper's LCL setting (Table 3b)
NORMS = ("week", "profile", "none")   # what a window is divided by
SEASONAL_DAYS = 30         # +/- days of the year a seasonal neighbour may sit
SEASONAL_MIN_LAG = 180     # ... and how far back it must be: a year earlier
SEASON = 365               # days in the seasonal cycle
# The paper's 100 quantile levels, 0.01 .. 1.00 (eq. 15).
PAPER_LEVELS = tuple(i / 100 for i in range(1, 101))


# ---------------------------------------------------------------------------
# The daily matrix: household x NZ date x 48 local slots

@dataclass(frozen=True)
class Calendar:
    """NZ local days as UTC minutes, and where each UTC half-hour lands.

    bounds    (n_days + 1,) UTC minute of each local midnight
    slot      local clock slot of each UTC half-hour from bounds[0]
    expected  (n_days, 48) UTC half-hours per local slot: 1 on an ordinary
              day, 2 for the hour repeated when DST ends, 0 for the hour
              skipped when it starts
    """
    first_day: dt.date
    bounds: object
    slot: object
    expected: object

    @property
    def n_days(self) -> int:
        return len(self.bounds) - 1

    def date(self, index: int) -> dt.date:
        return self.first_day + dt.timedelta(days=int(index))


def calendar(first_day: dt.date, n_days: int, tz: str = NZ) -> Calendar:
    import numpy as np

    zone = ZoneInfo(tz)
    bounds = np.array([
        int(dt.datetime.combine(first_day + dt.timedelta(days=i), dt.time(0),
                                tzinfo=zone).timestamp()) // 60
        for i in range(n_days + 1)], dtype=np.int64)
    utc = np.arange(bounds[0], bounds[-1], 30)
    local = [dt.datetime.fromtimestamp(int(m) * 60, zone) for m in utc]
    slot = np.array([t.hour * 2 + t.minute // 30 for t in local],
                    dtype=np.int64)
    day = np.searchsorted(bounds, utc, side="right") - 1
    expected = np.bincount(day * SLOTS + slot, minlength=n_days * SLOTS)
    return Calendar(first_day, bounds, slot, expected.reshape(n_days, SLOTS))


def day_matrix(cal: Calendar, starts, kwh, registers: int = 1):
    """One household's import reads -> (values (n_days, 48), complete (n_days,)).

    `starts` are UTC minutes since the epoch; reads outside the calendar are
    ignored. `registers` is how many import registers each half-hour should
    have read.

    A day is complete when every slot was read, except:
      * the two slots DST skips, which are interpolated;
      * an isolated missing slot, read on both sides within the day, which is
        interpolated -- the paper's rule (section 3.1).
    The hour DST repeats holds the sum of its two readings. Any other gap, or
    a slot read by only some of the registers, makes the day incomplete, and an
    incomplete day is never a query, a window or a continuation.
    """
    import numpy as np

    starts = np.asarray(starts, dtype=np.int64)
    kwh = np.asarray(kwh, dtype=float)
    inside = ((starts >= cal.bounds[0]) & (starts < cal.bounds[-1])
              & ((starts - cal.bounds[0]) % 30 == 0))
    starts, kwh = starts[inside], kwh[inside]
    day = np.searchsorted(cal.bounds, starts, side="right") - 1
    cell = day * SLOTS + cal.slot[(starts - cal.bounds[0]) // 30]

    size = cal.n_days * SLOTS
    values = np.bincount(cell, weights=kwh, minlength=size).reshape(-1, SLOTS)
    count = np.bincount(cell, minlength=size).reshape(-1, SLOTS)
    expected = cal.expected * registers

    read = (expected > 0) & (count == expected)
    partial = (count > 0) & (count != expected)
    missing = (expected > 0) & (count == 0)
    read_left = np.zeros_like(read)
    read_left[:, 1:] = read[:, :-1]
    read_right = np.zeros_like(read)
    read_right[:, :-1] = read[:, 1:]
    isolated = missing & read_left & read_right

    complete = ~(partial | (missing & ~isolated)).any(axis=1)
    fill = complete[:, None] & ((expected == 0) | isolated)
    for d in np.flatnonzero(fill.any(axis=1)):
        have = np.flatnonzero(read[d])
        gaps = np.flatnonzero(fill[d])
        values[d, gaps] = np.interp(gaps, have, values[d, have])
    return values, complete


# ---------------------------------------------------------------------------
# The library of windows, and the neighbours

@dataclass(frozen=True)
class Library:
    """Every usable (window, continuation) pair across the cohort, in
    last_day order, so the pairs usable before a day are a prefix.

    windows        (n, 7 x 48) each divided by its own maximum
    continuations  (n, horizon x 48) divided by the same maximum
    household      (n,) row of the cohort matrix it came from
    start          (n,) day index of the window's first day
    last_day       (n,) day index of the continuation's last day; the pair is
                   usable for target day d only when last_day < d
    """
    windows: object
    continuations: object
    household: object
    start: object
    last_day: object
    sq_norm: object


def window_library(values, complete, horizon: int = 1,
                   window_days: int = WINDOW_DAYS) -> Library:
    """Windows with a 1-day step, over every household.

    `values` is (households, days, 48) and `complete` (households, days).
    A pair needs all window_days + horizon days complete. A window whose
    maximum is 0 has no shape to compare and is left out.
    """
    import numpy as np

    values = np.asarray(values, dtype=float)
    complete = np.asarray(complete, dtype=bool)
    span = window_days + horizon
    run = np.concatenate(
        [np.zeros((complete.shape[0], 1), dtype=np.int64),
         np.cumsum(complete, axis=1)], axis=1)
    usable = (run[:, span:] - run[:, :-span]) == span
    household, start = np.nonzero(usable)

    block = values[household[:, None], start[:, None] + np.arange(span)]
    windows = block[:, :window_days].reshape(len(start), -1)
    scale = windows.max(axis=1)
    keep = scale > 0
    order = np.flatnonzero(keep)[np.argsort(start[keep], kind="stable")]
    windows = windows[order] / scale[order, None]
    continuations = (block[order, window_days:].reshape(len(order), -1)
                     / scale[order, None])
    return Library(
        windows=windows,
        continuations=continuations,
        household=household[order],
        start=start[order],
        last_day=start[order] + span - 1,
        sq_norm=(windows ** 2).sum(axis=1),
    )


def scale_of(rows, norm: str = "week"):
    """(n,) the divisor each row is compared on.

    'week' is the paper's: the week's largest half-hour. 'profile' is its
    mean, which compares the shape and not the size. 'none' compares raw
    kWh. A row whose scale is 0 has no shape, and its callers drop it.
    """
    import numpy as np

    rows = np.asarray(rows, dtype=float)
    if norm == "week":
        return rows.max(axis=1)
    if norm == "profile":
        return rows.mean(axis=1)
    if norm == "none":
        return np.ones(len(rows))
    raise ValueError(f"norm must be one of {NORMS}, not {norm!r}")


def normalise(queries, norm: str = "week"):
    """Each row divided by its own scale -> (normalised, scale).

    An all-zero row stays zero, and its forecast is zero after rescaling.
    """
    import numpy as np

    queries = np.asarray(queries, dtype=float)
    scale = scale_of(queries, norm)
    return queries / np.where(scale > 0, scale, 1.0)[:, None], scale


def neighbours(library: Library, normalised, before: int, k: int = PAPER_K):
    """(queries, k) library rows nearest each normalised query, nearest first.

    Only rows with last_day < before are candidates: that is the leakage
    rule. Within the k, equal distances keep library order, and the same
    input always selects the same rows.
    """
    import numpy as np

    usable = int(np.searchsorted(library.last_day, before, side="left"))
    if usable < k:
        raise ValueError(f"only {usable} usable windows before day "
                         f"{before}; k={k} needs more")
    q = np.asarray(normalised, dtype=float)
    d2 = ((q ** 2).sum(axis=1)[:, None] + library.sq_norm[None, :usable]
          - 2.0 * q @ library.windows[:usable].T)
    part = np.argpartition(d2, k - 1, axis=1)[:, :k]
    part.sort(axis=1)
    order = np.argsort(np.take_along_axis(d2, part, axis=1), axis=1,
                       kind="stable")
    return np.take_along_axis(part, order, axis=1)


def knn_forecast(library: Library, queries, before: int, k: int = PAPER_K):
    """(queries, k, horizon x 48) ensemble, in the queries' own units.

    `queries` are raw (queries, 7 x 48) windows ending the day before
    `before`.
    """
    normalised, scale = normalise(queries)
    rows = neighbours(library, normalised, before, k)
    return library.continuations[rows] * scale[:, None, None]


# ---------------------------------------------------------------------------
# Month-end: each neighbour carries its whole continuation (Stage 2)
#
# The paper forecasts one day ahead; a bill needs the rest of the cycle. Summing
# per-day quantiles would be wrong -- the sum of the P90s is not the P90 of the
# sum -- so each neighbour's continuation is summed first, and the quantiles
# are taken over those k totals.

@dataclass(frozen=True)
class TrajectoryLibrary:
    """Every complete week across the cohort, in start order, with what the
    household went on to use over each of the next r days.

    windows      (n, 7 x 48) each divided by its own maximum
    household    (n,) row of the cohort matrix it came from
    start        (n,) day index of the window's first day, ascending
    totals       (n, max_horizon + 1) kWh over the r days after the window,
                 divided by the window's maximum; NaN unless all r days are
                 in the calendar and complete. totals[:, 0] is 0.
    window_days  days in a window
    """
    windows: object
    sq_norm: object
    household: object
    start: object
    totals: object
    window_days: int


def trajectory_library(values, complete, max_horizon: int,
                       window_days: int = WINDOW_DAYS,
                       norm: str = "week") -> TrajectoryLibrary:
    """Weeks with a 1-day step, over every household.

    A week needs all its days complete and a scale above 0. Its total for
    horizon r is usable only when the r days after it are complete too; one
    week can serve a short horizon and not a long one. `norm` is what the
    week and its totals are divided by (scale_of()); a household left out of
    `complete` contributes no weeks, which is how a smaller pool is built.
    """
    import numpy as np

    values = np.asarray(values, dtype=float)
    complete = np.asarray(complete, dtype=bool)
    n_h, n_d = complete.shape
    run = np.concatenate([np.zeros((n_h, 1), dtype=np.int64),
                          np.cumsum(complete, axis=1)], axis=1)
    whole = (run[:, window_days:] - run[:, :-window_days]) == window_days
    household, start = np.nonzero(whole)
    order = np.argsort(start, kind="stable")
    household, start = household[order], start[order]

    windows = values[household[:, None],
                     start[:, None] + np.arange(window_days)].reshape(
                         len(start), -1)
    scale = scale_of(windows, norm)
    keep = scale > 0
    household, start, scale = household[keep], start[keep], scale[keep]
    windows = windows[keep] / scale[:, None]

    kwh = np.where(complete, values.sum(axis=2), 0.0)
    cum = np.concatenate([np.zeros((n_h, 1)), np.cumsum(kwh, axis=1)], axis=1)
    first = start + window_days              # first day after the window
    horizons = np.arange(max_horizon + 1)
    end = first[:, None] + horizons[None, :]  # exclusive
    inside = end <= n_d
    end = np.minimum(end, n_d)
    h = household[:, None]
    all_read = (run[h, end] - run[household, first][:, None]) == horizons
    total = cum[h, end] - cum[household, first][:, None]
    return TrajectoryLibrary(
        windows=windows,
        sq_norm=(windows ** 2).sum(axis=1),
        household=household,
        start=start,
        totals=np.where(inside & all_read, total / scale[:, None], np.nan),
        window_days=window_days,
    )


def seasonal_rows(library: TrajectoryLibrary, as_at: int,
                  days: int = SEASONAL_DAYS, min_lag: int = SEASONAL_MIN_LAG):
    """(n,) True for the weeks whose continuation starts within `days` of
    day `as_at`'s time of year, a year or more earlier.

    The paper uses no weather. This is the backtest's stand-in: a neighbour
    drawn from the same season cannot be a July week standing in for a
    January one. `min_lag` keeps the recent weeks out, so "the same time of
    year" means the previous year and not the past fortnight; over a 13-month
    span that leaves exactly one season to draw from. Day indices are
    consecutive calendar days, so the arithmetic is in days: a leap year
    shifts a neighbour by one day inside a 30-day window.
    """
    import numpy as np

    lag = int(as_at) - (library.start + library.window_days)
    into_year = lag % SEASON
    return (lag >= min_lag) & (np.minimum(into_year,
                                          SEASON - into_year) <= days)


def trajectory_candidates(library: TrajectoryLibrary, as_at: int,
                          horizon: int, allowed=None):
    """(n,) True for the weeks usable at the end of day `as_at` for a
    `horizon`-day continuation.

    The leakage rule: the week AND its continuation end on or before as_at,
    for every household, and every continuation day is complete. `allowed`
    narrows the library further, as seasonal_rows() does.
    """
    import numpy as np

    last_start = as_at - library.window_days - horizon + 1
    usable = int(np.searchsorted(library.start, last_start, side="right"))
    mask = np.zeros(len(library.start), dtype=bool)
    mask[:usable] = ~np.isnan(library.totals[:usable, horizon])
    if allowed is not None:
        mask &= np.asarray(allowed, dtype=bool)
    return mask


def trajectory_neighbours(library: TrajectoryLibrary, normalised, as_at: int,
                          horizons, k: int = PAPER_K, allowed=None):
    """(queries, k) library rows nearest each normalised query, nearest
    first, each drawn only from the weeks trajectory_candidates() allows for
    that query's horizon. Equal distances keep library order.
    """
    import numpy as np

    q = np.asarray(normalised, dtype=float)
    horizons = [int(r) for r in horizons]
    masks = {r: trajectory_candidates(library, as_at, r, allowed)
             for r in set(horizons)}
    width = max(int(np.flatnonzero(m)[-1]) + 1 if m.any() else 0
                for m in masks.values())
    d2 = ((q ** 2).sum(axis=1)[:, None] + library.sq_norm[None, :width]
          - 2.0 * q @ library.windows[:width].T)
    rows = np.empty((len(horizons), k), dtype=np.int64)
    for i, r in enumerate(horizons):
        idx = np.flatnonzero(masks[r][:width])
        if len(idx) < k:
            raise ValueError(f"only {len(idx)} usable weeks for a {r}-day "
                             f"continuation at day {as_at}; k={k} needs more")
        dist = d2[i, idx]
        part = np.argpartition(dist, k - 1)[:k]
        part.sort()
        order = np.argsort(dist[part], kind="stable")
        rows[i] = idx[part[order]]
    return rows


def remaining_ensemble(library: TrajectoryLibrary, queries, as_at: int,
                       horizons, k: int = PAPER_K, norm: str = "week",
                       allowed=None):
    """(queries, k) kWh still to come after day `as_at`, one per neighbour.

    `queries` are raw (queries, 7 x 48) weeks ending on as_at, and horizons[i]
    is how many days query i's cycle still has to run. Each neighbour's
    horizon-day total is rescaled by the query week's own scale, which
    `norm` names. `allowed` narrows the library, as seasonal_rows() does.
    """
    import numpy as np

    normalised, scale = normalise(queries, norm)
    rows = trajectory_neighbours(library, normalised, as_at, horizons, k,
                                 allowed)
    horizons = np.asarray(horizons, dtype=np.int64)
    return library.totals[rows, horizons[:, None]] * scale[:, None]


# ---------------------------------------------------------------------------
# The paper's two baselines (section 2.2.5), for household h on day d

def historical_sampling(values, complete, h: int, d: int):
    """Every earlier complete day of h on d's weekday -> (members, 48).

    "The predicted quantiles of the consumption of a Monday at 15:00 are
    computed using the consumption of all previous Mondays at 15:00."
    """
    import numpy as np

    days = np.arange(d % 7, d, 7)
    return np.asarray(values)[h, days[np.asarray(complete)[h, days]]]


def persistence(values, complete, h: int, d: int):
    """Yesterday plus every earlier day-on-day change -> (members, 48).

    The point forecast is day d-1; the spread is the empirical distribution
    of that forecast's own past errors, slot by slot, from day pairs that end
    before d. Members are clipped at zero, since a meter reads no less; that
    can only lower this baseline's CRPS.
    """
    import numpy as np

    values = np.asarray(values)
    complete = np.asarray(complete)
    days = np.arange(1, d)
    days = days[complete[h, days] & complete[h, days - 1]]
    errors = values[h, days] - values[h, days - 1]
    return np.maximum(values[h, d - 1] + errors, 0.0)


# ---------------------------------------------------------------------------
# Scores. Every function takes members on the LAST axis of `ensemble` and
# broadcasts `observed` against the others.

def crps_ensemble(ensemble, observed):
    """Exact CRPS of an equally weighted ensemble: E|X - y| - E|X - X'| / 2.

    For one member it is the absolute error, which is what lets a point
    forecast be scored on the same scale. The pair term uses the sorted
    identity sum_ij |x_i - x_j| = 2 sum_i (2i - m + 1) x_(i).
    """
    import numpy as np

    x = np.sort(np.asarray(ensemble, dtype=float), axis=-1)
    y = np.asarray(observed, dtype=float)[..., None]
    m = x.shape[-1]
    weights = 2.0 * np.arange(m) - m + 1.0
    return np.abs(x - y).mean(axis=-1) - (x * weights).sum(axis=-1) / m ** 2


def quantile_crps(ensemble, observed, levels=PAPER_LEVELS):
    """The paper's discretised CRPS (eqs. 5-6): pinball loss of the
    ensemble's empirical quantiles, averaged over `levels`.

    The paper sums rather than averages; a constant factor leaves every ratio
    between models unchanged. Quantiles are numpy's default (linear), as the
    paper's nanquantile().
    """
    import numpy as np

    q_levels = np.asarray(levels, dtype=float)
    quantiles = np.quantile(np.asarray(ensemble, dtype=float), q_levels,
                            axis=-1)
    diff = np.asarray(observed, dtype=float)[None] - quantiles
    shape = (-1,) + (1,) * (diff.ndim - 1)
    q_levels = q_levels.reshape(shape)
    return np.maximum(q_levels * diff, (q_levels - 1.0) * diff).mean(axis=0)


def in_band(ensemble, observed, lower: float = 0.1, upper: float = 0.9):
    """True where the observation lies inside the ensemble's [lower, upper]
    empirical quantiles. Its mean is the band's coverage."""
    import numpy as np

    lo, hi = np.quantile(np.asarray(ensemble, dtype=float), [lower, upper],
                         axis=-1)
    y = np.asarray(observed, dtype=float)
    return (y >= lo) & (y <= hi)
