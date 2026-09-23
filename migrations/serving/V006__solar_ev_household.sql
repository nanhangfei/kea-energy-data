-- =====================================================================
-- THE HOUSEHOLD WITH BOTH PANELS AND AN EV  --  detect-v2
--
-- Sprint 4 covered solar households and EV households, never one that is
-- both. A meter records import and export on two separate, non-negative
-- registers and NEVER records generation, so for a generating ICP:
--
--   exporting half-hour      import reads 0. Load is INVISIBLE.
--   daylight, not exporting  import = load - pv. Load is UNDERSTATED by
--                            an amount nothing on the meter reveals.
--
-- A 7 kW wallbox under a generating array can read as 2 kW of import, which
-- detect-v1 silently published as a portable charger ('low_power'). This
-- migration is the serving half of refusing that claim: the marts now carry
-- how much of a day, and of a run, the panels hid, and a class the meter
-- cannot establish is published as NULL rather than guessed.
--
-- ⚠️ NULL charger_class IS NOT "NO EV FOUND". The session is published
-- either way; only the wallbox-vs-portable distinction is withheld. A reader
-- that branched on two classes needs a third branch. See the backend brief
-- in docs/sprints/sprint-4/data-plan.md.
--
-- Detected kWh, ev_half_hours and detected_class are UNCHANGED by masking,
-- deliberately: mart_forecast_input and hdd-linear-v3 see exactly what they
-- saw under detect-v1, so there is no forecast change in this release.
--
-- Design: kea-data-platform-design.md §15.
--
-- ⚠️ EVERY NAME IS SCHEMA-QUALIFIED. V001 sets search_path, but a database
-- that baselined V001 never ran that SET, so nothing here may rely on it.
--
-- NO GRANTS. Both tables already exist with V001/V002's default privileges;
-- a new column inherits the table's grants.
--
-- NO NEW FOREIGN KEYS (so nothing here needs DEFERRABLE INITIALLY IMMEDIATE;
-- a later migration that does add one still does -- see V005's header).
--
-- BOTH NEW COLUMNS ARE NULLABLE, WITH NO DEFAULT. The rows already in these
-- tables were built by detect-v1, which did not measure masking, and NULL is
-- that fact; backfilling 0 would assert "the panels hid nothing" about days
-- nobody looked at, which is the exact habit this work removes. Both tables
-- are TRUNCATE-and-load, so the first detect-v2 publication leaves no NULL
-- behind. Every CHECK below therefore tolerates NULL.
-- =====================================================================


-- ---------------------------------------------------------------------
-- mart_daily_detected_load -- how much of the DAY the meter could not see
--
-- A property of the day, not of the runs in it: a day with no detection at
-- all can still be one the panels hid, and a reader deciding how much to
-- trust "nothing found here" needs to know that.
-- ---------------------------------------------------------------------
ALTER TABLE kea.mart_daily_detected_load
    ADD COLUMN masked_intervals smallint;

COMMENT ON COLUMN kea.mart_daily_detected_load.masked_intervals IS
    'detect-v2: half-hours of this day the import register could not see in '
    'full -- exporting, or between the day''s first and last export. 0 for '
    'every household without panels and for every night. NULL on rows built '
    'before detect-v2.';

-- ev_charger_class was tied to ev_half_hours in both directions. The "no
-- detection means no label" half stands; the "a detection always means a
-- label" half does not, now that the day's largest run may be one the panels
-- hid. ev_confidence is unaffected: a masked run is capped at 'low', never
-- emptied, so confidence is still null exactly when nothing was detected.
ALTER TABLE kea.mart_daily_detected_load
    DROP CONSTRAINT detected_ev_labelled;
ALTER TABLE kea.mart_daily_detected_load
    ADD CONSTRAINT detected_ev_labelled
        CHECK ((ev_half_hours = 0) = (ev_confidence IS NULL)
           AND (ev_half_hours > 0 OR ev_charger_class IS NULL)
           AND (ev_half_hours > 0 OR ev_kwh_high = 0));

ALTER TABLE kea.mart_daily_detected_load
    ADD CONSTRAINT detected_masked_in_day
        CHECK (masked_intervals BETWEEN 0 AND 50);

-- ⚠️ NO CONSTRAINT PAIRING masked_intervals WITH ev_charger_class HERE, AND
-- THAT IS DELIBERATE. masked_intervals counts every half-hour of the DAY the
-- panels hid, run or no run; ev_charger_class describes the day's largest EV
-- RUN. A household that exports at noon and charges at 23:00 has a masked day
-- and a perfectly readable charge, and 'low_power' is the true answer for it.
-- The claim masking really does forbid is a run-grain one, and it is enforced
-- at run grain by charging_session_masking on kea.mart_charging_session below,
-- where masked_half_hours counts only the session's own half-hours.


-- ---------------------------------------------------------------------
-- mart_charging_session -- how much of the RUN the meter could not see
-- ---------------------------------------------------------------------
ALTER TABLE kea.mart_charging_session
    ADD COLUMN masked_half_hours smallint;

COMMENT ON COLUMN kea.mart_charging_session.masked_half_hours IS
    'detect-v2: half-hours of this session the panels hid. Above zero, a '
    'shaved wallbox and a portable charger are the same signal, so '
    'charger_class is NULL and confidence is low. NULL on rows built before '
    'detect-v2.';

ALTER TABLE kea.mart_charging_session
    ALTER COLUMN charger_class DROP NOT NULL;

COMMENT ON COLUMN kea.mart_charging_session.charger_class IS
    '⚠️ NULLABLE since detect-v2. NULL means the panels hid part of the '
    'session and the two charger kinds cannot be told apart -- NOT that no '
    'EV was found.';

-- Re-stated rather than left implicit: a bare IN () already admits NULL, and
-- a constraint sitting beside a column that has just lost NOT NULL should say
-- out loud that the unknown case is intended.
ALTER TABLE kea.mart_charging_session
    DROP CONSTRAINT charging_session_class;
ALTER TABLE kea.mart_charging_session
    ADD CONSTRAINT charging_session_class
        CHECK (charger_class IS NULL
            OR charger_class IN ('high_power', 'low_power'));

ALTER TABLE kea.mart_charging_session
    ADD CONSTRAINT charging_session_masking
        CHECK (masked_half_hours BETWEEN 0 AND half_hours
           AND (masked_half_hours = 0
                OR (confidence = 'low'
                    AND (charger_class IS NULL
                      OR charger_class = 'high_power'))));


-- ---------------------------------------------------------------------
-- ev_scenario -- the recommendation a solar+EV household actually wants
--
-- Nulling charger_class on exactly the sessions that matter would REMOVE EV
-- advice from the households with most to gain (build_ev_scenarios.py filters
-- on 'high_power'), so the new pattern is what makes the change safe to ship:
--
--   solar_soak  charge into your own generation. add_ev places the day's
--               session in the half-hours the meter actually exported, most
--               first; shift_detected moves a detected session's kWh into
--               them, capped at each half-hour's own measured export.
--
-- Offered only when the basis window really did export, and priced by the
-- existing charge_half_hour(), which already charges from export first.
--
-- ⚠️ solar_soak MAY CARRY A NEGATIVE SAVING, so it is added to the patterns a
-- saving is allowed for. On Time of Use, unsoaked midday import sits in the
-- most expensive window and the honest answer is sometimes "your night rate
-- still wins". A losing row is published, not suppressed.
-- ---------------------------------------------------------------------
ALTER TABLE kea.ev_scenario
    DROP CONSTRAINT ev_scenario_pattern;
ALTER TABLE kea.ev_scenario
    ADD CONSTRAINT ev_scenario_pattern
        CHECK ((scenario = 'add_ev'
                AND charging_pattern IN ('evening', 'night', 'solar_soak'))
            OR (scenario = 'shift_detected'
                AND charging_pattern IN ('as_detected', 'night', 'solar_soak')));

ALTER TABLE kea.ev_scenario
    DROP CONSTRAINT ev_scenario_baseline_saves_nothing;
ALTER TABLE kea.ev_scenario
    ADD CONSTRAINT ev_scenario_baseline_saves_nothing
        CHECK (charging_pattern IN ('night', 'solar_soak')
            OR saving_vs_baseline_nzd = 0);
