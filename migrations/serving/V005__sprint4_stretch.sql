-- =====================================================================
-- SPRINT 4 -- the three stretch goals of Scenario 10
--
--   appliance-level breakdown   kea.mart_daily_detected_load
--                               kea.mart_charging_session
--                               + a detected-load bill driver
--                               + forecast hdd-linear-v3's input
--   solar payback, EV shifting  kea.solar_estimate, kea.ev_scenario
--   hardship early warning      three new reason codes, kea.hardship_outlook
--
-- Design: kea-data-platform-design.md §15. Graduated from, or replacing,
-- drafts in kea_deferred_schema.sql; see that file's header for which.
--
-- ⚠️ EVERY NAME IS SCHEMA-QUALIFIED. V001 sets search_path, but a database
-- that baselined V001 never ran that SET, so nothing here may rely on it.
--
-- NO GRANTS. V001/V002 set default privileges for new tables in kea
-- (kea_app SELECT; kea_loader SELECT/INSERT/UPDATE/DELETE and sequence
-- USAGE), and the same migrator creates these tables. The read contract test
-- (pipeline/tests/test_serving_publication.py) checks kea_app can read them.
--
-- ⚠️ EVERY NEW FOREIGN KEY IS DEFERRABLE INITIALLY IMMEDIATE. A publication
-- replaces billing.account and catalogue.plan under SET CONSTRAINTS ALL
-- DEFERRED. V003 converted the keys that existed when it ran and never sees a
-- later table, so a key declared here without it would break the publish the
-- same way V003 describes. The contract test also asserts that no key in the
-- served schemas is NOT DEFERRABLE.
-- =====================================================================


-- ---------------------------------------------------------------------
-- Enumerations
-- ---------------------------------------------------------------------

-- How sure a detection is. ASCENDING, so `confidence >= 'medium'` reads the
-- way it sounds: enum comparisons follow declaration order.
CREATE TYPE common.detection_confidence AS ENUM ('low', 'medium', 'high');

-- Early-warning codes. Like the five v1 codes, each is a fact about the
-- account, never a weight. None of them makes an outreach on its own:
-- create_hardship_alerts.py rule v2 needs bill_above_usual_payment TOGETHER
-- with payment_drift or first_shortfall.
--
--   payment_drift             each of the last three paid bills was paid
--                             later than the one before, still under a week
--                             late -- before late_payment_streak can fire
--   bill_above_usual_payment  the latest bill, or the open cycle's forecast,
--                             is well above what the account usually pays
--   first_shortfall           the first part payment after a run of bills
--                             paid in full -- before part_payment_arrears,
--                             which needs two
--
-- ADD VALUE runs inside the migration's transaction (PostgreSQL 12+) because
-- nothing in this file uses the new values; a value cannot be used until the
-- transaction that added it commits. V001's comment that ADD VALUE "cannot run
-- inside a transaction block" predates PostgreSQL 12.
ALTER TYPE common.hardship_signal_code ADD VALUE IF NOT EXISTS 'payment_drift';
ALTER TYPE common.hardship_signal_code ADD VALUE IF NOT EXISTS 'bill_above_usual_payment';
ALTER TYPE common.hardship_signal_code ADD VALUE IF NOT EXISTS 'first_shortfall';


-- ---------------------------------------------------------------------
-- mart_daily_detected_load                           (export load, TRUNCATE)
--
-- EVIDENCE, NOT A BREAKDOWN. Daily totals cannot show a load that is only
-- visible half-hour by half-hour: an EV session, a hot-water timer. This
-- carries what the half-hourly import shows, per NZ day, and the backend
-- composes the breakdown from it (kea_deferred_schema.sql's
-- load_decomposition stays deferred for that reason).
--
-- PER DAY, NOT PER MONTH, because bills do not follow calendar months: any
-- billing period is a plain sum over its days, as mart_daily_usage is.
--
-- ONE ROW FOR EVERY ASSESSED DAY -- a day on which the uncontrolled import
-- register read all expected_intervals half-hours -- with zeros when nothing
-- was detected. No row means "not assessed", which is a different answer
-- from "assessed, nothing there".
--
-- ⚠️ A RANGE, NEVER ONE FIGURE. For each half-hour of a published run:
--   low   import minus the level just before and after the run
--   high  import minus the day's floor (floor_kwh_per_hh)
-- The floor itself belongs to base load, so high never counts a kWh the
-- base-load driver already counts. Each half-hour counts on its own date,
-- so a run across midnight is split between two rows.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_daily_detected_load (
    icp_id                  bigint        NOT NULL,
    usage_date              date          NOT NULL,

    -- mart_daily_usage.base_load_kwh / 48: the day's smallest import half-hour.
    floor_kwh_per_hh        numeric(10,4) NOT NULL,

    -- A load with the charging pattern of an EV. NOT "your EV": the meter
    -- sees a pattern, not a vehicle.
    ev_kwh_low              numeric(12,4) NOT NULL DEFAULT 0,
    ev_kwh_high             numeric(12,4) NOT NULL DEFAULT 0,
    ev_half_hours           smallint      NOT NULL DEFAULT 0,
    -- From the day's largest run: high_power is a wallbox (5 kW and up),
    -- low_power a portable charger. Never higher than medium confidence.
    ev_charger_class        text,
    ev_confidence           common.detection_confidence,

    -- A load that switches on at the same time most nights, all year round:
    -- often a water heater on a timer. A block that runs only in winter is
    -- NOT published here; it is left to the heating slope.
    timed_night_kwh_low     numeric(12,4) NOT NULL DEFAULT 0,
    timed_night_kwh_high    numeric(12,4) NOT NULL DEFAULT 0,
    timed_night_half_hours  smallint      NOT NULL DEFAULT 0,
    timed_night_confidence  common.detection_confidence,

    -- The dbt var detect_method_version. A changed threshold is a new version.
    method_version          text          NOT NULL,

    built_at                timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, usage_date),
    CONSTRAINT detected_floor_non_negative CHECK (floor_kwh_per_hh >= 0),
    CONSTRAINT detected_ev_bounds
        CHECK (ev_kwh_low >= 0 AND ev_kwh_low <= ev_kwh_high),
    CONSTRAINT detected_night_bounds
        CHECK (timed_night_kwh_low >= 0 AND timed_night_kwh_low <= timed_night_kwh_high),
    CONSTRAINT detected_half_hours_in_day
        CHECK (ev_half_hours BETWEEN 0 AND 50 AND timed_night_half_hours BETWEEN 0 AND 50),
    -- Nothing detected means zeros and no labels; something detected means
    -- both labels. "Confidence null exactly when nothing was detected."
    CONSTRAINT detected_ev_labelled
        CHECK ((ev_half_hours = 0) = (ev_confidence IS NULL)
           AND (ev_half_hours = 0) = (ev_charger_class IS NULL)
           AND (ev_half_hours > 0 OR ev_kwh_high = 0)),
    CONSTRAINT detected_night_labelled
        CHECK ((timed_night_half_hours = 0) = (timed_night_confidence IS NULL)
           AND (timed_night_half_hours > 0 OR timed_night_kwh_high = 0)),
    CONSTRAINT detected_ev_class
        CHECK (ev_charger_class IN ('high_power', 'low_power'))
);


-- ---------------------------------------------------------------------
-- mart_charging_session                              (export load, TRUNCATE)
--
-- The EV-pattern runs themselves, for the shift-to-night scenario and for
-- staff. A run is consecutive half-hours on the continuous timeline, so a
-- missing read ends it, and its length in absolute time is exactly
-- half_hours x 30 minutes, across a DST change too. No two sessions of one
-- ICP overlap: a dbt test, since a CHECK cannot compare rows.
-- ---------------------------------------------------------------------
CREATE TABLE kea.mart_charging_session (
    icp_id          bigint        NOT NULL,
    -- Start of the first half-hour, end of the last (exclusive). UTC, like
    -- metering.meter_read_hh.interval_start.
    session_start   timestamptz   NOT NULL,
    session_end     timestamptz   NOT NULL,
    half_hours      smallint      NOT NULL,
    kwh_low         numeric(12,4) NOT NULL,
    kwh_high        numeric(12,4) NOT NULL,
    charger_class   text          NOT NULL,
    confidence      common.detection_confidence NOT NULL,
    method_version  text          NOT NULL,
    built_at        timestamptz   NOT NULL DEFAULT now(),
    PRIMARY KEY (icp_id, session_start),
    CONSTRAINT charging_session_length
        CHECK (half_hours > 0
           AND session_end = session_start + half_hours * interval '30 minutes'),
    CONSTRAINT charging_session_bounds CHECK (kwh_low >= 0 AND kwh_low <= kwh_high),
    CONSTRAINT charging_session_class CHECK (charger_class IN ('high_power', 'low_power'))
);


-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THESE TWO TO serving.TABLES.
--
-- Written inside the publication by build_solar_estimates.py and
-- build_ev_scenarios.py, like plan_recommendation, and reset only by
-- rebuild_demo.sh. computed_at is left to its DEFAULT, so every row of one
-- publication carries that transaction's time; publish_serving.validate()
-- relies on it to tell this publication's rows from history.
--
-- WHAT-IFS, NOT MEASUREMENTS. Every row names its assumptions_version
-- (pipeline/scripts/estimate_assumptions.py) and the metered window it
-- replays. Paybacks are simple: no panel degradation, no price change.
-- ---------------------------------------------------------------------

-- Solar payback, for an account with no export register today.
-- Output comes from a regional yield table (low and high kWh per kW per day,
-- by month), spread over daylight half-hours, netted against the account's
-- real half-hourly import and priced on its current plan.
CREATE TABLE kea.solar_estimate (
    estimate_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id              bigint        NOT NULL
                            REFERENCES billing.account(account_id) DEFERRABLE INITIALLY IMMEDIATE,
    -- The plan both costs were priced on: the account's plan when computed.
    plan_id                 integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    system_kw               numeric(4,1)  NOT NULL,
    -- The yield table's region (Auckland, Waikato, Wellington, Christchurch),
    -- so the answer can say whose sunshine it assumed.
    weather_region          text          NOT NULL,
    basis_from              date          NOT NULL,
    basis_to                date          NOT NULL,

    annual_generation_kwh_low   numeric(10,1) NOT NULL,
    annual_generation_kwh_high  numeric(10,1) NOT NULL,
    -- Share of the generation used in the home rather than exported.
    self_used_pct_low       numeric(5,2)  NOT NULL,
    self_used_pct_high      numeric(5,2)  NOT NULL,
    annual_saving_nzd_low   numeric(12,2) NOT NULL,
    annual_saving_nzd_high  numeric(12,2) NOT NULL,
    install_cost_nzd_low    numeric(12,2) NOT NULL,
    install_cost_nzd_high   numeric(12,2) NOT NULL,
    -- low = lowest cost / highest saving; high = highest cost / lowest
    -- saving. NULL when that saving is zero: there is no payback to state.
    -- Wide on purpose: a tiny positive saving gives a huge, honest figure,
    -- and the wording, not the column, decides how to say "never".
    payback_years_low       numeric(8,1),
    payback_years_high      numeric(8,1),

    assumptions_version     text          NOT NULL,
    computed_at             timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (account_id, system_kw, computed_at),
    CONSTRAINT solar_estimate_kw_positive CHECK (system_kw > 0),
    CONSTRAINT solar_estimate_basis_ordered CHECK (basis_to >= basis_from),
    CONSTRAINT solar_estimate_generation_bounds
        CHECK (annual_generation_kwh_low >= 0
           AND annual_generation_kwh_low <= annual_generation_kwh_high),
    CONSTRAINT solar_estimate_self_used_bounds
        CHECK (self_used_pct_low >= 0 AND self_used_pct_low <= self_used_pct_high
           AND self_used_pct_high <= 100),
    CONSTRAINT solar_estimate_saving_bounds
        CHECK (annual_saving_nzd_low <= annual_saving_nzd_high),
    CONSTRAINT solar_estimate_cost_bounds
        CHECK (install_cost_nzd_low > 0 AND install_cost_nzd_low <= install_cost_nzd_high),
    -- If even the best case has no payback, neither has the worst case.
    CONSTRAINT solar_estimate_payback_bounds
        CHECK ((payback_years_low IS NOT NULL OR payback_years_high IS NULL)
           AND payback_years_low >= 0
           AND payback_years_low <= payback_years_high)
);

CREATE INDEX idx_solar_estimate_account
    ON kea.solar_estimate(account_id, computed_at DESC, system_kw);

-- EV charging what-ifs.
--
--   add_ev          every cohort account: a year of charging from the
--                   assumed annual km, starting at 18:00 (evening, the
--                   baseline) or 23:00 (night), on each Kea plan
--   shift_detected  accounts with high_power sessions: each session's
--                   kwh_low as it happened (as_detected, the baseline) or
--                   moved to 23:00 the same night, on the current plan and
--                   on Time of Use. On a flat plan the saving is honestly 0.
--
-- annual_bill_nzd is the whole household's modelled annual bill with that
-- charging, so plans compare directly; annual_ev_cost_nzd is the part the
-- charging adds on that plan. saving_vs_baseline_nzd compares this row with
-- the same account, scenario and plan's baseline pattern.
CREATE TABLE kea.ev_scenario (
    scenario_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id              bigint        NOT NULL
                            REFERENCES billing.account(account_id) DEFERRABLE INITIALLY IMMEDIATE,
    scenario                text          NOT NULL,
    plan_id                 integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    current_plan_id         integer       NOT NULL
                            REFERENCES catalogue.plan(plan_id) DEFERRABLE INITIALLY IMMEDIATE,
    charging_pattern        text          NOT NULL,
    basis_from              date          NOT NULL,
    basis_to                date          NOT NULL,

    annual_ev_kwh           numeric(10,1) NOT NULL,
    annual_bill_nzd         numeric(12,2) NOT NULL,
    annual_ev_cost_nzd      numeric(12,2) NOT NULL,
    saving_vs_baseline_nzd  numeric(12,2) NOT NULL,

    assumptions_version     text          NOT NULL,
    computed_at             timestamptz   NOT NULL DEFAULT now(),
    UNIQUE (account_id, scenario, plan_id, charging_pattern, computed_at),
    CONSTRAINT ev_scenario_pattern
        CHECK ((scenario = 'add_ev'         AND charging_pattern IN ('evening', 'night'))
            OR (scenario = 'shift_detected' AND charging_pattern IN ('as_detected', 'night'))),
    CONSTRAINT ev_scenario_baseline_saves_nothing
        CHECK (charging_pattern = 'night' OR saving_vs_baseline_nzd = 0),
    CONSTRAINT ev_scenario_basis_ordered CHECK (basis_to >= basis_from),
    CONSTRAINT ev_scenario_kwh_non_negative CHECK (annual_ev_kwh >= 0)
);

CREATE INDEX idx_ev_scenario_account
    ON kea.ev_scenario(account_id, computed_at DESC, scenario);


-- ---------------------------------------------------------------------
-- ⚠️ APPEND-ONLY. NEVER ADD THIS TABLE TO serving.TABLES.
--
-- hardship_outlook -- the "weeks ahead" half of the early warning, written
-- by build_hardship_assessments.py (ruleset hardship-identification-v2) when
-- payment_drift or first_shortfall is present.
--
-- ⚠️ STILL NO SCORE. V001's rule stands: this is a date and an amount, with
-- the arithmetic that produced them. A staff member can check every step of
-- `basis`; nobody can check a weight.
--
-- One row per assessment at most, hence the key. A CHECK cannot read the
-- parent, so projected_arrears_on > assessed_on is asserted by the job and
-- by a Soda check.
-- ---------------------------------------------------------------------
CREATE TABLE kea.hardship_outlook (
    assessment_id           bigint        PRIMARY KEY
                            REFERENCES kea.hardship_assessment(assessment_id)
                            ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
    -- The first date the projected balance meets the arrears definition:
    -- a bill still at least 5 % unpaid 30 days after its due date.
    projected_arrears_on    date          NOT NULL,
    projected_overdue_nzd   numeric(12,2) NOT NULL,
    -- ⚠️ FOR STAFF, NEVER SHOWN TO THE CUSTOMER UNMEDIATED -- the same rule
    -- as hardship_signal.explanation.
    basis                   text          NOT NULL,
    CONSTRAINT hardship_outlook_overdue_positive CHECK (projected_overdue_nzd > 0)
);


-- ---------------------------------------------------------------------
-- The detected-load driver and forecast v3's input
--
-- Nullable, so both marts' existing rows and the backend's existing reads
-- are unaffected until the export fills them. The backend selects columns by
-- name, so appended columns do not disturb it.
-- ---------------------------------------------------------------------

-- mart_bill_explanation's attribution order becomes: period length, weather,
-- base load, DETECTED LOADS, tariff, unexplained. The heating slope is now
-- fitted net of detected loads, so a new EV lands here instead of in weather
-- or in delta_unexplained.
--   detected_load_kwh_day_*    mean of ev_kwh_low + timed_night_kwh_low
--                              over each period's assessed days
--   delta_from_detected_loads  (this - prior) x period_days, priced at the
--                              prior period's rate like the other drivers
ALTER TABLE kea.mart_bill_explanation
    ADD COLUMN detected_load_kwh_day_this  numeric(10,4),
    ADD COLUMN detected_load_kwh_day_prior numeric(10,4),
    ADD COLUMN delta_from_detected_loads   numeric(12,2);

-- hdd-linear-v3 adds this back to baseline_daily_kwh, because the baseline
-- is now fitted without detected loads: the mean of ev_kwh_low +
-- timed_night_kwh_low over the assessed days in the 28 days up to
-- as_at_date. NULL with fewer than 7 such days, and is_forecastable is then
-- false.
ALTER TABLE kea.mart_forecast_input
    ADD COLUMN detected_load_kwh_day       numeric(10,4);
