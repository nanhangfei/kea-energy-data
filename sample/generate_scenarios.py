# -*- coding: utf-8 -*-
"""Generate the bill_higher_*.sql fixtures in this directory.

    python3 data/sample/generate_scenarios.py

Each fixture answers one line of Kea's own "Why is my bill higher than
normal?" help page, and seeds TWO bills -- a normal period and the one that
follows -- because "higher than normal" is a comparison and a single bill
cannot express it.

The generated SQL is committed, so this script is not needed to USE the
fixtures. It exists so the scenarios can be retuned coherently: the machinery
below (rating, marts, driver decomposition, self-check) is identical in every
file, and only the SCENARIOS block differs. Editing six near-identical 750-line
files by hand would drift.

To add or change a scenario, edit SCENARIOS and EXPECT, re-run, and apply the
output to a throwaway database. Each fixture asserts its own outcome in a
closing DO block -- the bill must reconcile to its lines, the drivers must sum
to the delta, and the intended driver and hedging flag must come out right --
so a scenario that no longer demonstrates what it claims fails loudly rather
than seeding quietly wrong data.

No third-party imports: standard library only, so it runs anywhere.
"""
import os, datetime

# Emit next to this script, wherever the repo is checked out.
OUT = os.path.dirname(os.path.abspath(__file__))

def outfile(fx, stem):
    """Path for one generated fixture.

    The NN- prefix IS the fixture number -- the same digit that sits in the
    third position of every id the file seeds. One number to remember, not
    two: a row carrying account_id 226001 came out of 06-*.sql and nowhere
    else. Deriving the prefix from fx rather than a hand-kept table is what
    keeps that true; the two cannot drift apart.

    01 is the hand-written printed invoice, which is not generated here.
    """
    return os.path.join(OUT, "%02d-%s.sql" % (fx, stem))

# Half-hourly household shape, weights normalised per day.
WEIGHT = """CASE
               WHEN hr >= 0  AND hr < 6  THEN 0.55
               WHEN hr >= 6  AND hr < 9  THEN 1.60
               WHEN hr >= 9  AND hr < 12 THEN 0.80
               WHEN hr >= 12 AND hr < 16 THEN 0.70
               WHEN hr >= 16 AND hr < 18 THEN 1.10
               WHEN hr >= 18 AND hr < 21 THEN 1.80
               WHEN hr >= 21 AND hr < 23 THEN 1.00
               ELSE 0.65
           END"""

# Heating response, applied in every scenario: kWh added per heating degree
# day, spread across the hours a household actually heats. Sums to ~0.93.
HEAT = """CASE
               WHEN hr >= 6  AND hr < 9  THEN 0.040
               WHEN hr >= 17 AND hr < 22 THEN 0.045
               WHEN hr >= 22 OR  hr < 6  THEN 0.005
               ELSE 0.010
           END"""
HEAT_SENS = 0.93

# ---------------------------------------------------------------------
# Fixture ids.
#
# Every seeded primary key is <table><fixture><serial>, so an id read out of
# a query says which table it came from without a lookup. The old scheme gave
# every table in a fixture the SAME number -- 900001 was at once the customer,
# the ICP, the meter, the plan, the account and the bill -- which made a stray
# id in a result set unattributable, and let a DELETE key on the wrong table
# without anyone noticing.
#
#   11 8 001
#   |  | +--- serial within this table and fixture
#   |  +----- fixture number, 1..9
#   +-------- table; the first digit is the schema
#
# Keep this table in step with the one in data/README.md.
# ---------------------------------------------------------------------
TBL = dict(network=11, property=12, icp=13, meter=14, register=15,   # metering
           customer=21, account=22, bill=23,                          # billing
           plan=31, band=32, component=33)                            # catalogue


def oid(table, fixture, serial=1):
    """Seed id for one row: oid('bill', 9, 2) -> 239002."""
    assert 1 <= fixture <= 9 and 1 <= serial <= 999
    return TBL[table] * 10000 + fixture * 1000 + serial


def ids(fixture):
    """The ids every fixture needs, keyed as the templates expect them."""
    d = dict(fx=fixture,
             net=oid("network", fixture), prop=oid("property", fixture),
             icp_id=oid("icp", fixture), meter=oid("meter", fixture),
             reg=oid("register", fixture), cust=oid("customer", fixture),
             acct=oid("account", fixture), plan=oid("plan", fixture))
    # tou_band and tariff_component are separate tables and no longer share a
    # number: k* are band ids, m* are component ids.
    for i in range(1, 12):
        d["k%d" % i] = oid("band", fixture, i)
        d["m%d" % i] = oid("component", fixture, i)
    return d


LOSS_FACTOR = 0.0511
RATES = dict(net_peak=0.1361, net_offpeak=0.0378, daily=0.6000, transmission=0.0235,
             ene_peak=0.2600, ene_offpeak=0.1000, ene_shoulder=0.1400, levy=0.0013)


class S:
    def __init__(self, **kw):
        self.__dict__.update(kw)


SCENARIOS = [
    S(slug="full_house", fx=2,
      title="A FULL HOUSE — more people at home",
      net_code="ORON", net_name="Orion New Zealand", region="Canterbury", city="Christchurch",
      cust_no="212001", cust_name="Ana Whitiora",
      icp="DEMO0000000201AA", addr="12 Kowhai Street", suburb="Riccarton",
      prior=("2024-05-01", "2024-05-31"), curr=("2024-06-01", "2024-06-30"),
      prior_temp=12.4, curr_temp=10.2,
      prior_daily=9.40, curr_daily=9.40,
      # daytime occupancy: someone home 09:00-17:00, plus a little more
      # always-on draw (a second fridge, more devices left on standby)
      # Most of the extra load is DAYTIME occupancy, which no driver column
      # measures. The small all-day term is the extra always-on draw that
      # more people bring; keep it small or it dominates base load and the
      # fixture stops demonstrating the unexplained path.
      extra="""CASE WHEN period = 'current' AND hr >= 9 AND hr < 17 THEN 0.360
                    WHEN period = 'current'                        THEN 0.015
                    ELSE 0 END""",
      lede="A new baby arrived on 1 June and two family members moved in for the winter.",
      teaching="""The honest result here is a LARGE delta_unexplained, and that is the
-- point of this fixture. Occupancy has no column in mart_bill_explanation:
-- period length, weather, base load and tariff are the only drivers the
-- warehouse can measure. More people at home lifts DAYTIME consumption, which
-- none of those four capture, so the decomposition can only account for part
-- of the rise and is_explainable comes out false.
--
-- The agent should therefore say "I can account for $X of the $Y increase --
-- the rest is daytime usage between 9am and 5pm, has something changed at
-- home?" rather than asserting a cause. Compare kea.mart_hourly_profile
-- between May and June: the 09:00-17:00 bars roughly double while the
-- overnight bars barely move."""),

    S(slug="cold_snap", fx=3,
      title="CHANGE IN SEASON — a cold winter month after a mild autumn",
      net_code="WELL", net_name="Wellington Electricity", region="Wellington", city="Wellington",
      cust_no="213001", cust_name="Tama Rewiri",
      icp="DEMO0000000301AA", addr="8 Hataitai Road", suburb="Hataitai",
      prior=("2024-03-01", "2024-03-31"), curr=("2024-06-01", "2024-06-30"),
      prior_temp=17.8, curr_temp=8.6,
      prior_daily=8.20, curr_daily=8.20,
      extra="0",
      lede="Nothing changed in the house. March was mild; June was cold and the heat pump ran.",
      teaching="""This is the cleanest attribution of the six: the household's non-heating
-- load is IDENTICAL in both periods (8.20 kWh/day), and every extra kilowatt
-- hour comes from the heating term, which is driven by heating degree days.
-- delta_from_weather should dominate and delta_unexplained should be close to
-- zero.
--
-- Note the periods are 31 and 30 days, so delta_from_period_length is slightly
-- NEGATIVE -- the shorter month pushes the bill down while the weather pushes
-- it up. An explainer that only reports the largest driver would miss that."""),

    S(slug="faulty_appliance", fx=4,
      title="SOMETHING'S BROKEN — a hot water cylinder element stuck on",
      net_code="PWRC", net_name="Powerco", region="Taranaki", city="New Plymouth",
      cust_no="214001", cust_name="Mere Kahu",
      icp="DEMO0000000401AA", addr="41 Devon Street", suburb="Fitzroy",
      prior=("2024-05-01", "2024-05-31"), curr=("2024-06-01", "2024-06-30"),
      prior_temp=12.9, curr_temp=10.6,
      prior_daily=9.00, curr_daily=9.00,
      # a fault draws the same load every half hour, day and night, from the
      # day it fails until it is fixed
      extra="""CASE WHEN d >= date '2024-06-08' THEN 0.220 ELSE 0 END""",
      lede="The cylinder thermostat failed on 8 June and the element has been heating around the clock.",
      teaching="""A fault is the one cause that shows up cleanly in base load, because it
-- draws the SAME power at 3am as at 3pm. kea.mart_daily_usage.base_load_kwh --
-- the quietest half hour of the day, scaled to 24 hours -- steps up on 8 June
-- and never comes back down. delta_from_base_load should dominate.
--
-- Contrast this with the new-appliance fixture, which adds a similar number of
-- kilowatt hours but leaves base load untouched. Same size increase, opposite
-- signature. If the explainer cannot tell these two apart it will tell a
-- customer with a broken cylinder to check their heating habits."""),

    S(slug="new_appliance", fx=5,
      title="A NEW APPLIANCE — a power-hungry addition running on a timer",
      net_code="AURA", net_name="Aurora Energy", region="Otago", city="Dunedin",
      cust_no="215001", cust_name="Josh Fuller",
      icp="DEMO0000000501AA", addr="27 Highgate", suburb="Roslyn",
      prior=("2024-05-01", "2024-05-31"), curr=("2024-06-01", "2024-06-30"),
      prior_temp=11.2, curr_temp=8.9,
      prior_daily=9.00, curr_daily=9.00,
      # a dryer / EV charger on a nightly timer, 21:00-23:00 only
      extra="""CASE WHEN d >= date '2024-06-05' AND hr >= 21 AND hr < 23
                    THEN 1.050 ELSE 0 END""",
      lede="A heat-pump dryer arrived on 5 June and runs on a timer from 9pm every night.",
      teaching="""The deliberate contrast with the faulty-appliance fixture. This adds a
-- comparable number of kilowatt hours, but all of it lands in a two-hour
-- window, so the overnight minimum -- and therefore base_load_kwh -- does not
-- move at all. delta_from_base_load stays near zero and the rise falls into
-- delta_unexplained.
--
-- What identifies it is kea.mart_hourly_profile: a single tall pair of bars at
-- 21:00 and 22:00 in June that are absent in May. That is the signal the
-- explainer needs to reach for when base load is flat but consumption is not.
-- The load also sits in the energy SHOULDER band (21:00-23:00), so the cost
-- per kWh is higher than the same energy would cost after 11pm -- worth the
-- agent mentioning as a shiftable saving."""),

    S(slug="estimated_reads", fx=6,
      title="OTHER BILL CHANGES — the period contains estimated reads",
      net_code="UNSN", net_name="Unison Networks", region="Hawke's Bay", city="Napier",
      cust_no="216001", cust_name="Priya Raman",
      icp="DEMO0000000601AA", addr="3 Marine Parade", suburb="Napier South",
      prior=("2024-05-01", "2024-05-31"), curr=("2024-06-01", "2024-06-30"),
      prior_temp=13.6, curr_temp=11.1,
      prior_daily=8.80, curr_daily=9.00,
      # the meter stopped reporting on 11 June; estimates were substituted and
      # they run high
      extra="""CASE WHEN d >= date '2024-06-11' THEN 0.075 ELSE 0 END""",
      estimated_from="2024-06-11",
      missing_day="2024-06-10",
      lede="The meter stopped communicating on 10 June. Reads from 11 June on are estimates, and they run high.",
      teaching="""Nothing is wrong with this household. Two thirds of the period was
-- ESTIMATED rather than measured, and the estimates over-read.
--
-- This fixture exists to exercise the hedging path, and it sets a trap. The
-- estimates over-read by a constant amount every half hour, so the
-- decomposition attributes most of the rise to BASE LOAD -- it looks exactly
-- like the faulty-cylinder fixture. An explainer that reads the drivers and
-- stops would tell this customer they have something stuck on.
--
-- What stops it is estimated_read_pct being well above zero and
-- missing_interval_count being non-zero, which force is_explainable to false
-- NO MATTER how cleanly the drivers add up. The agent must not assert a cause
-- from numbers the meter never produced. The correct answer is roughly "two
-- thirds of this bill is estimated, so I would not read too much into the
-- increase -- a real read will true it up."
--
-- metering.meter_read_hh.quality is 'estimated' for those intervals, and 10
-- June is missing entirely, so completeness checks have something to find."""),

    S(slug="longer_period", fx=7,
      title="OTHER BILL CHANGES — the bill covers a longer period than usual",
      net_code="TPCO", net_name="Top Energy", region="Northland", city="Kerikeri",
      cust_no="217001", cust_name="Daniel Okafor",
      icp="DEMO0000000701AA", addr="15 Kerikeri Road", suburb="Kerikeri",
      prior=("2024-04-01", "2024-04-30"), curr=("2024-05-01", "2024-06-12"),
      prior_temp=16.1, curr_temp=13.2,
      prior_daily=9.60, curr_daily=9.70,
      extra="0",
      lede="A billing run was delayed, so this invoice covers 43 days instead of the usual 30.",
      teaching="""The customer used very slightly MORE per day (9.70 against 9.60), but the
-- bill is up by nearly half. Almost all of it is the extra 13 days -- both the
-- variable charges and 13 more days of fixed charge.
--
-- period_days is a GENERATED column on billing.bill precisely so this check
-- cannot be skipped: it is the first thing the explainer must look at. The
-- schema comment says so directly -- attributing a 43-day bill to cold weather
-- would be confidently wrong.
--
-- The useful answer quotes the daily rate, not the total: "your average day
-- cost $2.61 last time and $2.63 this time -- the bill is bigger because it
-- covers 43 days, not 30.\""""),
]


def days_between(a, b):
    da = datetime.date.fromisoformat(a)
    db = datetime.date.fromisoformat(b)
    return (db - da).days + 1


# ---------------------------------------------------------------------
# Blocks shared by every fixture in this file, extracted so the two
# templates below cannot drift apart. Both still carry {placeholders} and
# are formatted with the same dict as the template that embeds them.
# ---------------------------------------------------------------------
CATALOGUE_SRC = r'''-- ---------------------------------------------------------------------
-- Plan and tariff. Rates match the sample bill in
-- 01-bill_printed_invoice_2024-08.sql so figures across data/sample are
-- comparable; only the plan CODE differs, so that each scenario owns its own
-- catalogue rows and the files stay independent of one another.
-- ---------------------------------------------------------------------
INSERT INTO src_catalogue.price_plans
    (plan_id, code, name, network_code, user_category, is_tou,
     ppd_percent, valid_from, valid_to, active)
OVERRIDING SYSTEM VALUE
VALUES ({plan}, '{plan_code}', 'Demo Standard Fixed TOU',
        '{net_code}', 'STANDARD', true, 0, '2024-01-01', NULL, true)
ON CONFLICT (code, valid_from) DO UPDATE SET
    name = EXCLUDED.name, network_code = EXCLUDED.network_code, active = EXCLUDED.active;

-- Gapless, all seven days. Network peak runs 30 minutes later than energy
-- peak, the per-charge-group band scoping the design doc calls out in §8.2.1.
INSERT INTO src_catalogue.tou_windows
    (window_id, plan_id, charge_scope, band_label, days_csv, time_from, time_to)
OVERRIDING SYSTEM VALUE
VALUES
    ({k1},  {plan}, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    ({k2},  {plan}, 'NET', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:30'),
    ({k3},  {plan}, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    ({k4},  {plan}, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '11:00', '17:00'),
    ({k5},  {plan}, 'NET', 'OFFPEAK',  '1,2,3,4,5,6,7', '21:30', '24:00'),
    ({k6},  {plan}, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '07:00', '11:00'),
    ({k7},  {plan}, 'ENE', 'PEAK',     '1,2,3,4,5,6,7', '17:00', '21:00'),
    ({k8},  {plan}, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '11:00', '17:00'),
    ({k9},  {plan}, 'ENE', 'SHOULDER', '1,2,3,4,5,6,7', '21:00', '23:00'),
    ({k10}, {plan}, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '00:00', '07:00'),
    ({k11}, {plan}, 'ENE', 'OFFPEAK',  '1,2,3,4,5,6,7', '23:00', '24:00');

INSERT INTO src_catalogue.rate_components
    (component_id, plan_id, charge_scope, component_desc, rate_uom,
     window_id, flow_direction, unit_rate, valid_from)
OVERRIDING SYSTEM VALUE
VALUES
    ({m1},  {plan}, 'NET', 'Peak Usage',           'KWH', {k1},  'import', {r_net_peak}, '2024-01-01'),
    ({m2},  {plan}, 'NET', 'Off Peak Usage',       'KWH', {k3},  'import', {r_net_offpeak}, '2024-01-01'),
    ({m3},  {plan}, 'NET', '{net_code} + Daily charges', 'DAY', NULL, 'import', {r_daily}, '2024-01-01'),
    ({m4},  {plan}, 'NET', 'Transmission Charges', 'KWH', NULL,  'import', {r_transmission}, '2024-01-01'),
    ({m5},  {plan}, 'ENE', 'Peak Usage',           'KWH', {k6},  'import', {r_ene_peak}, '2024-01-01'),
    ({m6},  {plan}, 'ENE', 'Off Peak Usage',       'KWH', {k10}, 'import', {r_ene_offpeak}, '2024-01-01'),
    ({m7},  {plan}, 'ENE', 'Shoulder Usage',       'KWH', {k8},  'import', {r_ene_shoulder}, '2024-01-01'),
    ({m11}, {plan}, 'OTH', 'ELECTRICITY AUTHORITY Levy', 'KWH', NULL, 'import', {r_levy}, '2024-01-01');

INSERT INTO src_billing.accounts
    (ACCT_ID, ACCT_NO, CUST_ID, ICP_NUMBER, PLAN_CODE, PAY_METHOD, BILL_CYCLE_DAY, START_DT)
OVERRIDING SYSTEM VALUE
VALUES ({acct}, 'DEMO-ACCT-{acct}', {cust}, '{icp}', '{plan_code}', 'DD', 28, '2019-01-01')
ON CONFLICT (ACCT_NO) DO UPDATE SET
    CUST_ID = EXCLUDED.CUST_ID, ICP_NUMBER = EXCLUDED.ICP_NUMBER,
    PLAN_CODE = EXCLUDED.PLAN_CODE;'''

REPLICA_SQL = r'''INSERT INTO metering.network (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES ({net}, '{net_code}', '{net_name}', '{region}')
ON CONFLICT (network_code) DO UPDATE SET network_name = EXCLUDED.network_name;

INSERT INTO billing.customer
    (customer_id, customer_number, full_name, email, is_medically_dependent, is_vulnerable)
OVERRIDING SYSTEM VALUE
VALUES ({cust}, '{cust_no}', '{cust_name}', NULL, false, false)
ON CONFLICT (customer_number) DO UPDATE SET full_name = EXCLUDED.full_name;

INSERT INTO metering.property (property_id, address_line_1, suburb, city, climate_region)
OVERRIDING SYSTEM VALUE
VALUES ({prop}, '{addr}', '{suburb}', '{city}', '{region}');

INSERT INTO metering.icp (icp_id, icp_number, property_id, network_id, connection_date)
OVERRIDING SYSTEM VALUE
VALUES ({icp_id}, '{icp}', {prop}, {net}, '2019-01-01')
ON CONFLICT (icp_number) DO UPDATE SET
    property_id = EXCLUDED.property_id, network_id = EXCLUDED.network_id;

INSERT INTO metering.meter_register
    (register_id, icp_id, register_code, description, is_controlled, direction)
OVERRIDING SYSTEM VALUE
VALUES ({reg}, {icp_id}, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET icp_id = EXCLUDED.icp_id;

-- Resolve the source feed into serving surrogate keys. read_status 'E' in the
-- source becomes quality 'estimated' here; that mapping is what lets the
-- explainer know how much of a period it can actually stand behind.
INSERT INTO metering.meter_read_hh
    (icp_id, register_id, interval_start, kwh, quality, source_file)
SELECT {icp_id}, {reg}, interval_start, kwh,
       CASE read_status WHEN 'E' THEN 'estimated'::common.read_quality
                        ELSE 'actual'::common.read_quality END,
       source_file
FROM src_metering.interval_reads
WHERE icp_identifier = '{icp}';

INSERT INTO catalogue.plan
    (plan_id, plan_code, plan_name, network_id, user_type, is_tou,
     prompt_payment_discount_pct, effective_from, is_active)
OVERRIDING SYSTEM VALUE
VALUES ({plan}, '{plan_code}', 'Demo Standard Fixed TOU', {net}, 'standard', true,
        0, '2024-01-01', true)
ON CONFLICT (plan_code, effective_from) DO UPDATE SET
    plan_name = EXCLUDED.plan_name, network_id = EXCLUDED.network_id;

INSERT INTO catalogue.tou_band
    (band_id, plan_id, charge_group, band_name, days_of_week, start_time, end_time)
OVERRIDING SYSTEM VALUE
VALUES
    ({k1},  {plan}, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    ({k2},  {plan}, 'network', 'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:30'),
    ({k3},  {plan}, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    ({k4},  {plan}, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    ({k5},  {plan}, 'network', 'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:30', '24:00'),
    ({k6},  {plan}, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '07:00', '11:00'),
    ({k7},  {plan}, 'energy',  'peak',     ARRAY[1,2,3,4,5,6,7]::smallint[], '17:00', '21:00'),
    ({k8},  {plan}, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '11:00', '17:00'),
    ({k9},  {plan}, 'energy',  'shoulder', ARRAY[1,2,3,4,5,6,7]::smallint[], '21:00', '23:00'),
    ({k10}, {plan}, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '00:00', '07:00'),
    ({k11}, {plan}, 'energy',  'off_peak', ARRAY[1,2,3,4,5,6,7]::smallint[], '23:00', '24:00');

INSERT INTO catalogue.tariff_component
    (component_id, plan_id, charge_group, component_name, basis, band_id, direction, rate, effective_from)
OVERRIDING SYSTEM VALUE
VALUES
    ({m1},  {plan}, 'network', 'Peak Usage',     'per_kwh', {k1},  'import', {r_net_peak}, '2024-01-01'),
    ({m2},  {plan}, 'network', 'Off Peak Usage', 'per_kwh', {k3},  'import', {r_net_offpeak}, '2024-01-01'),
    ({m3},  {plan}, 'network', '{net_code} + Daily charges', 'per_day', NULL, 'import', {r_daily}, '2024-01-01'),
    ({m4},  {plan}, 'network', 'Transmission Charges', 'per_kwh', NULL, 'import', {r_transmission}, '2024-01-01'),
    ({m5},  {plan}, 'energy',  'Peak Usage',     'per_kwh', {k6},  'import', {r_ene_peak}, '2024-01-01'),
    ({m6},  {plan}, 'energy',  'Off Peak Usage', 'per_kwh', {k10}, 'import', {r_ene_offpeak}, '2024-01-01'),
    ({m7},  {plan}, 'energy',  'Shoulder Usage', 'per_kwh', {k8},  'import', {r_ene_shoulder}, '2024-01-01'),
    ({m11}, {plan}, 'other',   'ELECTRICITY AUTHORITY Levy', 'per_kwh', NULL, 'import', {r_levy}, '2024-01-01');

INSERT INTO billing.account
    (account_id, account_number, customer_id, icp_id, plan_id, payment_method,
     billing_cycle_day, start_date)
OVERRIDING SYSTEM VALUE
VALUES ({acct}, 'DEMO-ACCT-{acct}', {cust}, {icp_id}, {plan}, 'direct_debit', 28, '2019-01-01')
ON CONFLICT (account_number) DO UPDATE SET
    customer_id = EXCLUDED.customer_id, icp_id = EXCLUDED.icp_id, plan_id = EXCLUDED.plan_id;'''


TEMPLATE = r'''-- =====================================================================
-- Kea Energy — bill-increase scenario seed
-- {title}
-- =====================================================================
--
-- {lede}
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number ({fx}) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHAT THE EXPLAINER MAY SAY
-- This file names a cause, because a generator knows what it generated. The
-- pipeline does not know, and must not claim to: data/README.md is explicit
-- that half-hourly interval data cannot identify an appliance, and calls the
-- refusal deliberate. The scenario name is ground truth for testing, never an
-- inference the agent is licensed to make.
--
-- What the agent may do is describe the SHAPE it can actually measure -- "a
-- constant draw around the clock", "a load between 9pm and 11pm", "your usage
-- between 9am and 5pm has roughly doubled" -- and then ask. Naming the dryer
-- is the customer's job, not the model's.
--
-- WHY TWO BILLS
-- "Higher than normal" is a comparison, so a single bill cannot express it.
-- Each scenario seeds a normal period and the period that follows it, and
-- kea.mart_bill_explanation.prior_bill_id links them. The prior bill is
-- deliberately left unexplained (is_explainable = false) because nothing
-- precedes it.
--
-- WHAT THIS FIXTURE TEACHES
-- {teaching}
--
-- PERIODS
--   prior    {p_from} .. {p_to}  ({p_days} days)
--   current  {c_from} .. {c_to}  ({c_days} days)
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the bill from the reads and raises if
-- the two disagree, so a silent drift fails the transaction.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = '{icp}';
DELETE FROM metering.meter_read_hh      WHERE icp_id = {icp_id};
DELETE FROM src_billing.invoice_lines   WHERE INV_ID IN ({bill_p}, {bill_c});
DELETE FROM src_billing.invoices        WHERE INV_ID IN ({bill_p}, {bill_c});
DELETE FROM src_billing.accounts        WHERE ACCT_ID = {acct};
DELETE FROM src_billing.customers       WHERE CUST_ID = {cust};
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN {k1} AND {k11};
DELETE FROM src_catalogue.price_plans     WHERE plan_id = {plan};
DELETE FROM src_metering.registers        WHERE register_id = {reg};
DELETE FROM src_metering.meters           WHERE meter_id = {meter};
DELETE FROM src_metering.connection_points WHERE icp_id = {icp_id};
DELETE FROM src_metering.networks         WHERE network_id = {net};

DELETE FROM kea.mart_bill_explanation WHERE bill_id IN ({bill_p}, {bill_c});
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = {icp_id};
DELETE FROM kea.mart_daily_usage      WHERE icp_id = {icp_id};
DELETE FROM kea.mart_account_context  WHERE account_id = {acct};
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-{slug}';
DELETE FROM billing.bill_line WHERE bill_id IN ({bill_p}, {bill_c});
DELETE FROM billing.bill      WHERE bill_id IN ({bill_p}, {bill_c});
DELETE FROM billing.account   WHERE account_id = {acct};
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN {k1} AND {k11};
DELETE FROM catalogue.plan             WHERE plan_id = {plan};
DELETE FROM metering.meter_register WHERE register_id = {reg};
DELETE FROM metering.icp            WHERE icp_id = {icp_id};
DELETE FROM metering.property       WHERE property_id = {prop};
DELETE FROM metering.network        WHERE network_id = {net};
DELETE FROM billing.customer        WHERE customer_id = {cust};

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES ({cust}, '{cust_no}', '{cust_name}', NULL, NULL,
        '{addr}', '{suburb}', '{city}', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES ({net}, '{net_code}', '{net_name}', '{region}')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES ({icp_id}, '{icp}', {net}, '{addr}, {suburb}',
        '{suburb}', '{city}', NULL, '{region}', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES ({meter}, {icp_id}, 'DEMO-METER-{meter}', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

-- Import only. These households have no solar, which keeps the fixture about
-- consumption: an export credit moving at the same time would muddy the
-- driver decomposition this file is here to demonstrate.
INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES ({reg}, {meter}, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- Half-hourly reads for BOTH periods.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--     + scenario_extra(day, hh)                            what this file is about
--
-- Weather is deterministic: a period mean temperature with a fixed daily
-- wobble, so heating degree days differ between the two periods by a known
-- amount and mart_bill_explanation.delta_from_weather can be checked by hand.
-- Every scenario carries the SAME heating response ({heat_sens} kWh per degree
-- day) so that weather is never the thing that distinguishes them, except in
-- the season fixture where the degree-day swing is the whole story.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Anchored to NZ LOCAL midnight via AT TIME ZONE rather than to a fixed
    -- +12 literal. +12 is NZST; between late September and early April the
    -- country is on NZDT (+13), so a literal offset starts the series an hour
    -- late and silently drops the first two half hours of the period. That is
    -- invisible in a total and only shows up as interval_count = 46 on a day
    -- that is not a DST day.
    SELECT generate_series(
               (date '{p_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '{p_to}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes') AS interval_start,
           'prior' AS period
    UNION ALL
    SELECT generate_series(
               (date '{c_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '{c_to}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes'),
           'current'
), tagged AS (
    SELECT interval_start, period,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    -- SIN() returns double precision, so the whole sum is cast back to
    -- numeric before rounding: round(double, int) has no overload.
    SELECT t.*,
           ROUND((({p_temp} + CASE WHEN period = 'current'
                                   THEN {c_temp} - {p_temp} ELSE 0 END)
                  + 2.2 * SIN(EXTRACT(day FROM d) * 0.7))::numeric, 2) AS temp_c
    FROM tagged t
), hdd AS (
    SELECT w.*, GREATEST(16.0 - temp_c, 0)::numeric AS hdd_day FROM weathered w
), shaped AS (
    SELECT h.*,
           {weight}::numeric AS w,
           CASE WHEN period = 'current' THEN {c_daily} ELSE {p_daily} END::numeric AS daily_kwh,
           {heat}::numeric * hdd_day AS heat_kwh,
           ({extra})::numeric AS extra_kwh
    FROM hdd h
), normed AS (
    SELECT s.*, SUM(w) OVER (PARTITION BY d) AS w_day FROM shaped s
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status, version_no, source_file)
SELECT '{icp}', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh + extra_kwh, 4),
       {read_status},
       1, 'sample-scenario-{slug}.csv'
FROM normed
{read_filter};

{catalogue_src}

INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
VALUES
    ({bill_p}, 'DEMO-INV-{bill_p}', {acct}, '{plan_code}', '{p_issue}', '{p_from}', '{p_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{p_due}', 0, NULL, 0, 0, 0, 0, 'ISSUED'),
    ({bill_c}, 'DEMO-INV-{bill_c}', {acct}, '{plan_code}', '{c_issue}', '{c_from}', '{c_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{c_due}', 0, NULL, 0, 0, 0, 0, 'ISSUED')
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

{replica}

INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
VALUES
    ({bill_p}, {acct}, 'DEMO-INV-{bill_p}', {plan}, '{p_issue}', '{p_from}', '{p_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{p_due}', 0, NULL, 0, 0, 0, 0, 'issued'),
    ({bill_c}, {acct}, 'DEMO-INV-{bill_c}', {plan}, '{c_issue}', '{c_from}', '{c_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{c_due}', 0, NULL, 0, 0, 0, 0, 'issued')
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads rather than transcribed.
--
-- The sample bill in this folder was typed from a printed invoice, because it
-- had to reproduce one exactly. These fixtures are generated, so the lines are
-- derived instead: band the half hours, sum them, apply the rate. The bill
-- therefore reconciles to the meter by construction, and any future change to
-- the shape of the reads flows straight through to the charges.
--
-- Line losses follow the sample bill's implied factor of {loss_pct}%; the
-- energy import amount is (quantity + loss) x rate.
-- ---------------------------------------------------------------------
WITH banded AS (
    SELECT b.bill_id, b.period_start, b.period_end,
           (b.period_end - b.period_start) + 1 AS days,
           r.kwh,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM billing.bill b
    JOIN metering.meter_read_hh r
      ON r.icp_id = {icp_id}
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.bill_id IN ({bill_p}, {bill_c})
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, {m1} AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, {r_net_peak}::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * {r_net_peak}, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, {m2}, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', {r_net_offpeak},
           NULL, ROUND((total_kwh - net_peak) * {r_net_offpeak}, 2), 20 FROM totals
    UNION ALL SELECT bill_id, {m3}, 'network', '{net_code} + Daily charges', NULL,
           days::numeric, 'per_day', {r_daily},
           NULL, ROUND(days * {r_daily}, 2), 30 FROM totals
    UNION ALL SELECT bill_id, {m4}, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', {r_transmission},
           NULL, ROUND(total_kwh * {r_transmission}, 2), 40 FROM totals
    UNION ALL SELECT bill_id, {m5}, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', {r_ene_peak}, ROUND(ene_peak * {loss}, 2),
           ROUND((ene_peak + ROUND(ene_peak * {loss}, 2)) * {r_ene_peak}, 2), 50 FROM totals
    UNION ALL SELECT bill_id, {m7}, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', {r_ene_shoulder}, ROUND(ene_shoulder * {loss}, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * {loss}, 2)) * {r_ene_shoulder}, 2), 60 FROM totals
    UNION ALL SELECT bill_id, {m6}, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', {r_ene_offpeak},
           ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2)) * {r_ene_offpeak}, 2), 70 FROM totals
    UNION ALL SELECT bill_id, {m11}, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
           total_kwh, 'per_kwh', {r_levy},
           NULL, ROUND(total_kwh * {r_levy}, 2), 80 FROM totals
)
INSERT INTO billing.bill_line
    (bill_id, component_id, charge_group, direction, line_label, band_name,
     quantity, unit, rate, loss_kwh, amount, sort_order)
SELECT bill_id, component_id, cg, 'import', label, band, qty, unit, rate, loss, amount, sort_order
FROM lines;

-- Mirror the rated lines back into the source invoice, so the source system
-- and the replica agree the way they would in production.
INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT bill_id,
       CASE charge_group WHEN 'network' THEN 'NET' WHEN 'energy' THEN 'ENE' ELSE 'OTH' END,
       'IMPORT', line_label, UPPER(band_name), quantity,
       CASE unit WHEN 'per_day' THEN 'DAY' ELSE 'KWH' END,
       rate, loss_kwh, amount, sort_order
FROM billing.bill_line WHERE bill_id IN ({bill_p}, {bill_c});

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id IN ({bill_p}, {bill_c}) GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID IN ({bill_p}, {bill_c}) GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
VALUES
    ({acct}, 'DEMO-ACCT-{acct}', {cust}, '{cust_no}', '{cust_name}', NULL,
     {icp_id}, '{icp}', '{addr}, {suburb}', '{region}', '{net_code}', '{net_name}',
     false, false, {plan}, '{plan_code}', 'Demo Standard Fixed TOU', 'standard', true,
     {r_daily}, 'direct_debit', 28, '{c_from}', '{c_to}', {c_days},
     {est_pct}, timestamptz '{c_to} 23:30:00+12', false)
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    estimated_read_pct   = EXCLUDED.estimated_read_pct,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage, derived from the reads. base_load_kwh is the quietest half hour
-- of the day scaled to 24 hours -- the always-on draw, and the figure the
-- faulty-appliance and new-appliance fixtures are designed to separate.
-- Weather is re-derived here with the same formula the reads were built from,
-- so mart and meter cannot drift apart.
--
-- Built from a DATE SPINE left-joined to the reads, not from the reads alone.
-- A day the meter never reported would otherwise be absent from the mart
-- entirely, and a row that does not exist cannot be counted as missing --
-- mart_bill_explanation.missing_interval_count would read 0 for a period with
-- a hole in it. With the spine, such a day lands with interval_count = 0 and
-- the gap is visible to anything that looks.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT {icp_id},
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       -- Never a fixed 48: a local day is 46 or 50 half hours across a DST
       -- change, and hardcoding 48 books phantom missing intervals on one and
       -- a NEGATIVE missing count on the other.
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       ROUND((({p_temp} + CASE WHEN sp.d >= date '{c_from}' THEN {c_temp} - {p_temp} ELSE 0 END)
              + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 2),
       ROUND(GREATEST(16.0 - (({p_temp} + CASE WHEN sp.d >= date '{c_from}' THEN {c_temp} - {p_temp} ELSE 0 END)
              + 2.2 * SIN(EXTRACT(day FROM sp.d) * 0.7))::numeric, 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.bill_id IN ({bill_p}, {bill_c})
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count, estimated_count = EXCLUDED.estimated_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

-- Hourly profile per calendar month. This is the mart that tells the
-- appliance scenarios apart: compare the same hour across the two months.
INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT {icp_id},
       date_trunc('month', d)::date,
       hour_of_day, is_weekday,
       -- Divided by HOURS OBSERVED, not by days. A local clock hour is not
       -- always an hour long: when New Zealand leaves daylight time the 2am
       -- hour runs twice and that date brings FOUR half hours to the group,
       -- and when it enters daylight time the 2am hour never happens and the
       -- date brings none. COUNT(DISTINCT d) books the doubled hour as a
       -- single day and inflates that hour's average -- 12.5% on the April
       -- weekend rows of the 13-month fixture, which spans both transitions.
       -- COUNT(DISTINCT interval_start) / 2 is the true number of hours behind
       -- the sum, so both transitions normalise themselves.
       ROUND(SUM(kwh) / NULLIF(COUNT(DISTINCT interval_start) / 2.0, 0), 4), 0,
       -- sample_days stays a DAY count on purpose. It answers "how much
       -- evidence is behind this bar", which is a different question from
       -- "what is the rate in this hour". On the spring-forward date the hour
       -- is genuinely absent, and one fewer sample day is the honest record.
       COUNT(DISTINCT d)::smallint
FROM (
    SELECT r.interval_start,
           (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')::smallint AS hour_of_day,
           EXTRACT(isodow FROM (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date) <= 5 AS is_weekday,
           r.kwh
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- Bill explanation.
--
-- The drivers are COMPUTED from the seeded data, in the attribution order the
-- schema lays out, each one measured against the prior period and converted to
-- dollars at the prior period's own average variable rate:
--
--   1 period length  extra days x prior daily kWh x rate, plus the fixed charge
--   2 weather        heating sensitivity x extra degree days x rate
--   3 base load      extra always-on kWh per day x days x rate
--   4 tariff         zero -- same plan, same rates in both periods
--   5 unexplained    whatever the first four do not account for
--
-- Sequential attribution, so nothing is counted twice: each driver is measured
-- after the ones above it have been taken out. Because the figures are derived
-- rather than typed, they stay true if the read shape is ever retuned.
-- ---------------------------------------------------------------------
WITH per_bill AS (
    SELECT b.bill_id, b.period_start, b.period_end, b.period_days,
           b.charges_excl_gst AS amount, b.total_import_kwh AS kwh,
           (SELECT ROUND(AVG(base_load_kwh), 4) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS base_day,
           (SELECT ROUND(SUM(heating_degree_days), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS hdd,
           (SELECT ROUND(AVG(temp_mean_c), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS temp,
           (SELECT COALESCE(SUM(estimated_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS est_intervals,
           (SELECT COALESCE(SUM(expected_intervals - interval_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS missing_intervals,
           (SELECT COALESCE(SUM(expected_intervals), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS expected_intervals
    FROM billing.bill b WHERE b.bill_id IN ({bill_p}, {bill_c})
), pair AS (
    SELECT c.*, p.bill_id AS prior_id, p.period_days AS prior_days,
           p.amount AS prior_amount, p.kwh AS prior_kwh,
           p.base_day AS prior_base, p.hdd AS prior_hdd, p.temp AS prior_temp,
           -- the prior period's own average variable rate: everything that
           -- scales with kWh, divided by the kWh it was charged on
           ROUND((p.amount - {r_daily} * p.period_days) / NULLIF(p.kwh, 0), 6) AS rate_per_kwh
    FROM per_bill c JOIN per_bill p ON p.bill_id = {bill_p}
    WHERE c.bill_id = {bill_c}
), attributed AS (
    SELECT pair.*,
           ROUND(pair.amount - pair.prior_amount, 2) AS d_amount,
           ROUND((pair.period_days - pair.prior_days)
                 * (pair.prior_kwh / NULLIF(pair.prior_days, 0) * pair.rate_per_kwh + {r_daily}), 2) AS d_period,
           ROUND({heat_sens} * (pair.hdd - pair.prior_hdd) * pair.rate_per_kwh, 2) AS d_weather,
           ROUND((pair.base_day - pair.prior_base) * pair.period_days * pair.rate_per_kwh, 2) AS d_base
    FROM pair
)
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, prior_bill_id, prior_period_days, prior_total_amount,
     prior_total_kwh, amount_delta, amount_delta_pct, kwh_delta,
     delta_from_period_length, delta_from_weather, delta_from_base_load,
     delta_from_tariff, delta_unexplained, hdd_this_period, hdd_prior_period,
     temp_mean_this, temp_mean_prior, base_load_kwh_day_this, base_load_kwh_day_prior,
     heating_sensitivity_kwh_per_hdd, estimated_read_pct, missing_interval_count,
     is_explainable)
SELECT bill_id, {acct}, {icp_id}, period_start, period_end, period_days,
       amount, kwh, prior_id, prior_days, prior_amount, prior_kwh,
       d_amount,
       ROUND(d_amount / NULLIF(prior_amount, 0) * 100, 2),
       ROUND(kwh - prior_kwh, 4),
       d_period, d_weather, d_base, 0.00,
       ROUND(d_amount - d_period - d_weather - d_base, 2),
       hdd, prior_hdd, temp, prior_temp, base_day, prior_base, {heat_sens},
       ROUND(est_intervals * 100.0 / NULLIF(expected_intervals, 0), 2),
       missing_intervals,
       -- Honest by construction: claim the increase is explained only when the
       -- measured drivers cover most of it AND the period was actually read.
       (ABS(d_period + d_weather + d_base) >= ABS(d_amount) * 0.60
        AND est_intervals = 0 AND missing_intervals = 0)
FROM attributed;

-- The prior bill has nothing before it, so no driver can be attributed.
-- is_explainable = false is the honest state: the agent should say "this is
-- the earliest bill I have" rather than assert a cause.
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, estimated_read_pct, missing_interval_count, is_explainable)
SELECT bill_id, {acct}, {icp_id}, period_start, period_end, period_days,
       charges_excl_gst, COALESCE(total_import_kwh, 0), 0, 0, false
FROM billing.bill WHERE bill_id = {bill_p};

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_{slug}', now(), now(), 2, 'sample-scenario-{slug}', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_prior numeric; v_curr numeric; v_delta numeric;
    v_kwh_p numeric; v_kwh_c numeric;
    v_lines numeric; v_drivers numeric; v_expl boolean;
    v_top text; v_share numeric;
BEGIN
    SELECT charges_excl_gst, total_import_kwh INTO v_prior, v_kwh_p
      FROM billing.bill WHERE bill_id = {bill_p};
    SELECT charges_excl_gst, total_import_kwh INTO v_curr, v_kwh_c
      FROM billing.bill WHERE bill_id = {bill_c};

    -- 1. the bill header must equal the sum of its own lines
    SELECT ROUND(SUM(amount), 2) INTO v_lines
      FROM billing.bill_line WHERE bill_id = {bill_c};
    IF v_lines <> v_curr THEN
        RAISE EXCEPTION 'current bill header % does not match its lines %', v_curr, v_lines;
    END IF;

    -- 2. the scenario must actually produce a HIGHER bill
    v_delta := ROUND(v_curr - v_prior, 2);
    IF v_delta <= 0 THEN
        RAISE EXCEPTION 'scenario {slug} did not raise the bill: prior %, current %',
                        v_prior, v_curr;
    END IF;

    -- 3. the five drivers must add up to the delta exactly
    SELECT ROUND(delta_from_period_length + delta_from_weather + delta_from_base_load
                 + delta_from_tariff + delta_unexplained, 2), is_explainable
      INTO v_drivers, v_expl
      FROM kea.mart_bill_explanation WHERE bill_id = {bill_c};
    IF v_drivers <> v_delta THEN
        RAISE EXCEPTION 'drivers sum to % but the delta is %', v_drivers, v_delta;
    END IF;

    -- 4. the driver this fixture exists to demonstrate must be the largest one
    SELECT driver, ROUND(amt / NULLIF(v_delta, 0) * 100, 1) INTO v_top, v_share
      FROM (SELECT 'period_length' AS driver, delta_from_period_length AS amt
              FROM kea.mart_bill_explanation WHERE bill_id = {bill_c}
            UNION ALL SELECT 'weather', delta_from_weather
              FROM kea.mart_bill_explanation WHERE bill_id = {bill_c}
            UNION ALL SELECT 'base_load', delta_from_base_load
              FROM kea.mart_bill_explanation WHERE bill_id = {bill_c}
            UNION ALL SELECT 'unexplained', delta_unexplained
              FROM kea.mart_bill_explanation WHERE bill_id = {bill_c}) z
     ORDER BY amt DESC LIMIT 1;
    IF v_top <> '{expect_driver}' THEN
        RAISE EXCEPTION
            'expected % to be the largest driver for {slug}, got % at % percent of the delta',
            '{expect_driver}', v_top, v_share;
    END IF;

    -- 5. and the hedging flag must land where the fixture intends
    IF v_expl <> {expect_expl} THEN
        RAISE EXCEPTION 'expected is_explainable = {expect_expl} for {slug}, got %', v_expl;
    END IF;

    RAISE NOTICE
        '{slug}: prior $%, current $% (up $%, % percent); % kWh -> % kWh; top driver % at % percent; explainable %',
        v_prior, v_curr, v_delta,
        ROUND(v_delta / NULLIF(v_prior, 0) * 100, 1),
        v_kwh_p, v_kwh_c, v_top, v_share, v_expl;
END $$;

COMMIT;

-- Verification queries:
-- SELECT period_start, period_end, period_days, charges_excl_gst, total_import_kwh
--   FROM billing.bill WHERE account_id = {acct} ORDER BY period_start;
-- SELECT amount_delta, delta_from_period_length, delta_from_weather,
--        delta_from_base_load, delta_unexplained, is_explainable
--   FROM kea.mart_bill_explanation WHERE bill_id = {bill_c};
-- SELECT profile_month, hour_of_day, avg_import_kwh
--   FROM kea.mart_hourly_profile WHERE icp_id = {icp_id} AND is_weekday
--   ORDER BY hour_of_day, profile_month;
'''


def issue_due(period_end):
    d = datetime.date.fromisoformat(period_end)
    issue = d + datetime.timedelta(days=1)
    return issue.isoformat(), (issue + datetime.timedelta(days=14)).isoformat()


# (largest driver, is_explainable) each fixture must produce. The second half
# matters as much as the first: estimated_reads is only useful if the flag
# comes out false despite a clean-looking decomposition.
EXPECT = dict(full_house=("unexplained", "false"), cold_snap=("weather", "true"),
              faulty_appliance=("base_load", "true"), new_appliance=("unexplained", "false"),
              estimated_reads=("base_load", "false"), longer_period=("period_length", "true"))

for s in SCENARIOS:
    p_issue, p_due = issue_due(s.prior[1])
    c_issue, c_due = issue_due(s.curr[1])

    # estimated / missing reads, only where the scenario calls for them
    read_status = "'A'"
    read_filter = ""
    est_pct = "0"
    if getattr(s, "estimated_from", None):
        read_status = ("CASE WHEN d >= date '%s' THEN 'E' ELSE 'A' END" % s.estimated_from)
        read_filter = "WHERE d <> date '%s'" % s.missing_day
        est_pct = "66.67"

    # Anything interpolated into a SQL string literal must have its quotes
    # doubled: 'Hawke's Bay' would otherwise close the literal early.
    def q(v):
        return v.replace("'", "''")

    f = dict(
        title=s.title, lede=s.lede, teaching=s.teaching, slug=s.slug,
        bill_p=oid("bill", s.fx, 1), bill_c=oid("bill", s.fx, 2),
        **ids(s.fx))
    f.update(
        net_code=q(s.net_code), net_name=q(s.net_name), region=q(s.region),
        city=q(s.city), cust_no=q(s.cust_no), cust_name=q(s.cust_name),
        icp=q(s.icp), addr=q(s.addr), suburb=q(s.suburb),
        plan_code="DEMO_STD_FXTOU_%s" % s.slug.upper(),
        p_from=s.prior[0], p_to=s.prior[1], c_from=s.curr[0], c_to=s.curr[1],
        p_days=days_between(*s.prior), c_days=days_between(*s.curr),
        p_issue=p_issue, p_due=p_due, c_issue=c_issue, c_due=c_due,
        p_temp=s.prior_temp, c_temp=s.curr_temp,
        p_daily=s.prior_daily, c_daily=s.curr_daily,
        weight=WEIGHT, heat=HEAT, heat_sens=HEAT_SENS, extra=s.extra,
        loss=LOSS_FACTOR, loss_pct=round(LOSS_FACTOR * 100, 2),
        read_status=read_status, read_filter=read_filter, est_pct=est_pct,
        expect_driver=EXPECT[s.slug][0], expect_expl=EXPECT[s.slug][1],
        **{("r_" + k): v for k, v in RATES.items()})

    f["catalogue_src"] = CATALOGUE_SRC.format(**f)
    f["replica"] = REPLICA_SQL.format(**f)

    path = outfile(s.fx, "bill_higher_%s" % s.slug)
    with open(path, "w") as fh:
        fh.write(TEMPLATE.format(**f))
    print("wrote", path)


# =====================================================================
# THIRTEEN-MONTH HISTORY FIXTURE
#
# The six scenarios above are two-period comparisons: one prior month, one
# current month, and a driver that separates them. That shape cannot answer
# "how does this winter compare with last winter", because it only ever holds
# two months of meter data.
#
# This fixture is the other shape. One household, THIRTEEN consecutive monthly
# bills and every half hour between them, so that anything reading
# metering.meter_read_hh has more than a full year to look at. The design doc
# says Postgres carries a 13-month subset of reads for the demo cohort; this is
# the file that actually demonstrates it.
#
# It reuses everything above -- the same half-hourly shape, the same heating
# response, the same rates, loss factor and rating SQL -- so the figures stay
# comparable with the other fixtures. The one thing it does differently is
# WEATHER: instead of a flat mean per period, temperature follows an annual
# sinusoid, so the heating term carries a real seasonal swing and the usage
# curve has a winter peak and a summer trough. Without that, a year of reads is
# just a flat line thirteen times longer, and nothing that reads it can
# demonstrate anything a single month could not.
# =====================================================================

# Auckland-ish annual temperature: mean, and half the peak-to-trough swing.
# Warmest around 15 January (doy 15), coldest six months later.
T_MEAN, T_AMP = 15.2, 4.6


def season_temp(d):
    """Temperature at date expression `d`: annual sinusoid plus the same
    fixed daily wobble the two-period fixtures use, so a single day is never
    exactly average and heating degree days vary day to day."""
    return ("ROUND((%.1f + %.1f * COS(2 * PI() * (EXTRACT(doy FROM %s) - 15) / 365.25)"
            " + 2.2 * SIN(EXTRACT(day FROM %s) * 0.7))::numeric, 2)"
            % (T_MEAN, T_AMP, d, d))


HISTORY = S(
    slug="13_month_history", fx=8,
    title="THIRTEEN MONTHS — a full year of half-hourly reads, plus one",
    net_code="AUNT", net_name="Aurora Energy", region="Otago", city="Dunedin",
    cust_no="218001", cust_name="Rangi Paterson",
    icp="DEMO0000000801AA", addr="27 Rattray Street", suburb="Mornington",
    # 13 calendar months, so the same month appears twice: August 2023 and
    # August 2024. That repetition is the point -- it is what makes a
    # year-on-year comparison possible at all.
    first_month="2023-08-01", last_month="2024-08-01",
    daily_kwh=8.50,
    lede="Thirteen consecutive monthly bills for one household, every half hour "
         "of meter data behind them, and a real seasonal swing between winter and summer.")


HISTORY_TEMPLATE = r'''-- =====================================================================
-- Kea Energy — {title}
-- =====================================================================
--
-- {lede}
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number ({fx}) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHAT IS IN HERE
--   {n_months} monthly bills                {hist_from} .. {hist_to}
--   {n_days} days of half-hourly reads      import only, one register
--   mart_daily_usage             one row per day, built from a date spine
--   mart_hourly_profile          {n_months} months x 24 hours x weekday/weekend
--   mart_bill_explanation        every bill except the first, against the one before it
--
-- WHY IT EXISTS
-- The six bill_higher_* fixtures each hold two months. Two months cannot
-- answer "how does this winter compare with last winter", and cannot exercise
-- anything that reads a year of history: seasonal normalisation, year-on-year
-- comparison, or a mart that groups by month and expects more than two.
-- This file holds thirteen, so the same calendar month appears twice.
--
-- WEATHER IS THE ONLY THING DONE DIFFERENTLY. The sibling fixtures use one
-- mean temperature per period. Here temperature follows an annual sinusoid --
-- mean {t_mean} degrees, swinging {t_amp} either side, warmest in mid-January --
-- so heating degree days rise into winter and fall away again, and the usage
-- curve has a real shape. A year of flat reads would be a single month
-- repeated thirteen times and would demonstrate nothing.
--
-- THE DST DAYS ARE REAL HERE. Thirteen months crosses both New Zealand clock
-- changes, so this is the only fixture in data/sample where the 46- and
-- 50-interval local days that metering.meter_read_hh's comment warns about
-- actually occur -- 24 September 2023 and 7 April 2024. expected_intervals is
-- therefore COMPUTED per day from the local day length, never assumed to be
-- 48, and the self-check fails if those two days are not present. Anything
-- that hardcodes 48 will report phantom missing intervals against this file,
-- which is exactly what it is here to catch.
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the fixture and raises on drift.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = '{icp}';
DELETE FROM metering.meter_read_hh      WHERE icp_id = {icp_id};
DELETE FROM src_billing.invoice_lines   WHERE INV_ID BETWEEN {bill_first} AND {bill_last};
DELETE FROM src_billing.invoices        WHERE INV_ID BETWEEN {bill_first} AND {bill_last};
DELETE FROM src_billing.accounts        WHERE ACCT_ID = {acct};
DELETE FROM src_billing.customers       WHERE CUST_ID = {cust};
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN {k1} AND {k11};
DELETE FROM src_catalogue.price_plans     WHERE plan_id = {plan};
DELETE FROM src_metering.registers        WHERE register_id = {reg};
DELETE FROM src_metering.meters           WHERE meter_id = {meter};
DELETE FROM src_metering.connection_points WHERE icp_id = {icp_id};
DELETE FROM src_metering.networks         WHERE network_id = {net};

DELETE FROM kea.mart_bill_explanation WHERE bill_id BETWEEN {bill_first} AND {bill_last};
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = {icp_id};
DELETE FROM kea.mart_daily_usage      WHERE icp_id = {icp_id};
DELETE FROM kea.mart_account_context  WHERE account_id = {acct};
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-{slug}';
DELETE FROM billing.bill_line WHERE bill_id BETWEEN {bill_first} AND {bill_last};
DELETE FROM billing.bill      WHERE bill_id BETWEEN {bill_first} AND {bill_last};
DELETE FROM billing.account   WHERE account_id = {acct};
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN {k1} AND {k11};
DELETE FROM catalogue.plan             WHERE plan_id = {plan};
DELETE FROM metering.meter_register WHERE register_id = {reg};
DELETE FROM metering.icp            WHERE icp_id = {icp_id};
DELETE FROM metering.property       WHERE property_id = {prop};
DELETE FROM metering.network        WHERE network_id = {net};
DELETE FROM billing.customer        WHERE customer_id = {cust};

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES ({cust}, '{cust_no}', '{cust_name}', NULL, NULL,
        '{addr}', '{suburb}', '{city}', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES ({net}, '{net_code}', '{net_name}', '{region}')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES ({icp_id}, '{icp}', {net}, '{addr}, {suburb}',
        '{suburb}', '{city}', NULL, '{region}', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES ({meter}, {icp_id}, 'DEMO-METER-{meter}', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

-- Import only, like the six comparison fixtures: no solar. A year of export
-- would be a second seasonal curve running opposite to the first, and this
-- file is about the length of the history, not about generation.
INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES ({reg}, {meter}, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- {n_days} days of half-hourly reads.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--
-- Same two terms as every other fixture, and the same {heat_sens} kWh per
-- degree day. The household term is CONSTANT at {daily_kwh} kWh/day across the
-- whole thirteen months -- nothing changes in this house. Every bit of the
-- seasonal swing comes from the heating term, which is what makes the fixture
-- honest: a year-on-year comparison over these reads should attribute almost
-- all of the difference to weather, because that is all there is.
--
-- generate_series steps in absolute time, so DST is handled by the timezone
-- conversion rather than by the series: the local day on 24 September 2023 has
-- 46 half hours and 7 April 2024 has 50. Normalising by SUM(w) OVER
-- (PARTITION BY d) -- the LOCAL date -- means the daily total still lands on
-- {daily_kwh} kWh on those days rather than being 4 percent out.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Both ends are anchored to NZ LOCAL midnight via AT TIME ZONE, not to a
    -- fixed +12 literal. +12 is NZST; from late September to early April the
    -- country is on NZDT (+13), so a literal offset silently starts the series
    -- an hour late and loses the first two half hours of the period. This
    -- fixture spans both, and would be wrong at whichever end fell in NZDT.
    SELECT generate_series(
        (date '{hist_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
        (date '{hist_to}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
            - interval '30 minutes',
        interval '30 minutes') AS interval_start
), tagged AS (
    SELECT interval_start,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    -- SIN()/COS() return double precision, so the whole sum is cast back to
    -- numeric before rounding: round(double, int) has no overload.
    SELECT t.*, {temp_d} AS temp_c
    FROM tagged t
), hdd AS (
    SELECT w.*, GREATEST(16.0 - temp_c, 0)::numeric AS hdd_day FROM weathered w
), shaped AS (
    SELECT h.*,
           {weight}::numeric AS w,
           {daily_kwh}::numeric AS daily_kwh,
           {heat}::numeric * hdd_day AS heat_kwh
    FROM hdd h
), normed AS (
    SELECT s.*, SUM(w) OVER (PARTITION BY d) AS w_day FROM shaped s
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status, version_no, source_file)
SELECT '{icp}', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh, 4),
       'A', 1, 'sample-scenario-{slug}.csv'
FROM normed;

{catalogue_src}

-- One invoice per calendar month, generated rather than listed. Issue date is
-- the day after the period ends; due date fourteen days after that.
INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
SELECT {bill_first} + n - 1,
       'INV-{acct}-' || to_char(m, 'YYYYMM'),
       {acct}, '{plan_code}',
       (m + interval '1 month')::date,
       m,
       (m + interval '1 month' - interval '1 day')::date,
       0, 0, 0, 0, 0.00, 0.00, 0,
       (m + interval '1 month' + interval '14 days')::date,
       0, NULL, 0, 0, 0, 0, 'ISSUED'
FROM (SELECT gs::date AS m, ROW_NUMBER() OVER (ORDER BY gs) AS n
      FROM generate_series(date '{first_month}', date '{last_month}', interval '1 month') gs) months
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

{replica}

-- The same thirteen periods on the serving side. period_days is a GENERATED
-- column, so it is never inserted -- it is derived from the two dates and
-- cannot disagree with them.
INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
SELECT {bill_first} + n - 1,
       {acct},
       'INV-{acct}-' || to_char(m, 'YYYYMM'),
       {plan},
       (m + interval '1 month')::date,
       m,
       (m + interval '1 month' - interval '1 day')::date,
       0, 0, 0, 0, 0.00, 0.00, 0,
       (m + interval '1 month' + interval '14 days')::date,
       0, NULL, 0, 0, 0, 0, 'issued'
FROM (SELECT gs::date AS m, ROW_NUMBER() OVER (ORDER BY gs) AS n
      FROM generate_series(date '{first_month}', date '{last_month}', interval '1 month') gs) months
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads -- the same SQL the two-period fixtures
-- use, with the bill filter widened from two ids to the whole account. The
-- aggregation was already per-bill, so thirteen months need no new logic.
--
-- Line losses follow the sample bill's implied factor of {loss_pct}%; the
-- energy import amount is (quantity + loss) x rate.
-- ---------------------------------------------------------------------
WITH banded AS (
    SELECT b.bill_id, b.period_start, b.period_end,
           (b.period_end - b.period_start) + 1 AS days,
           r.kwh,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM billing.bill b
    JOIN metering.meter_read_hh r
      ON r.icp_id = {icp_id}
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.account_id = {acct}
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, {m1} AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, {r_net_peak}::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * {r_net_peak}, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, {m2}, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', {r_net_offpeak},
           NULL, ROUND((total_kwh - net_peak) * {r_net_offpeak}, 2), 20 FROM totals
    UNION ALL SELECT bill_id, {m3}, 'network', '{net_code} + Daily charges', NULL,
           days::numeric, 'per_day', {r_daily},
           NULL, ROUND(days * {r_daily}, 2), 30 FROM totals
    UNION ALL SELECT bill_id, {m4}, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', {r_transmission},
           NULL, ROUND(total_kwh * {r_transmission}, 2), 40 FROM totals
    UNION ALL SELECT bill_id, {m5}, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', {r_ene_peak}, ROUND(ene_peak * {loss}, 2),
           ROUND((ene_peak + ROUND(ene_peak * {loss}, 2)) * {r_ene_peak}, 2), 50 FROM totals
    UNION ALL SELECT bill_id, {m7}, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', {r_ene_shoulder}, ROUND(ene_shoulder * {loss}, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * {loss}, 2)) * {r_ene_shoulder}, 2), 60 FROM totals
    UNION ALL SELECT bill_id, {m6}, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', {r_ene_offpeak},
           ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2)) * {r_ene_offpeak}, 2), 70 FROM totals
    UNION ALL SELECT bill_id, {m11}, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
           total_kwh, 'per_kwh', {r_levy},
           NULL, ROUND(total_kwh * {r_levy}, 2), 80 FROM totals
)
INSERT INTO billing.bill_line
    (bill_id, component_id, charge_group, direction, line_label, band_name,
     quantity, unit, rate, loss_kwh, amount, sort_order)
SELECT bill_id, component_id, cg, 'import', label, band, qty, unit, rate, loss, amount, sort_order
FROM lines;

INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT bill_id,
       CASE charge_group WHEN 'network' THEN 'NET' WHEN 'energy' THEN 'ENE' ELSE 'OTH' END,
       'IMPORT', line_label, UPPER(band_name), quantity,
       CASE unit WHEN 'per_day' THEN 'DAY' ELSE 'KWH' END,
       rate, loss_kwh, amount, sort_order
FROM billing.bill_line WHERE bill_id BETWEEN {bill_first} AND {bill_last};

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id BETWEEN {bill_first} AND {bill_last}
      GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID BETWEEN {bill_first} AND {bill_last}
      GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
SELECT {acct}, 'DEMO-ACCT-{acct}', {cust}, '{cust_no}', '{cust_name}', NULL,
       {icp_id}, '{icp}', '{addr}, {suburb}', '{region}', '{net_code}', '{net_name}',
       false, false, {plan}, '{plan_code}', 'Demo Standard Fixed TOU', 'standard', true,
       {r_daily}, 'direct_debit', 28,
       b.period_start, b.period_end, b.period_days, 0,
       (SELECT MAX(interval_start) FROM metering.meter_read_hh WHERE icp_id = {icp_id}),
       false
FROM billing.bill b
WHERE b.bill_id = {bill_last}
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage, one row per day for all {n_days} days, built from a DATE SPINE
-- left-joined to the reads so a day the meter never reported still lands with
-- interval_count = 0 rather than vanishing.
--
-- expected_intervals is COMPUTED from the length of the LOCAL day, not fixed
-- at 48. Over thirteen months that matters twice: 24 September 2023 is a
-- 46-interval day and 7 April 2024 is a 50-interval day. Hardcoding 48 would
-- book two missing intervals against a complete September day and two
-- phantom extra reads in April, and mart_bill_explanation would then refuse
-- to call those bills explainable because it checks missing_interval_count.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT {icp_id},
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       {temp_sp},
       ROUND(GREATEST(16.0 - {temp_sp}, 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.account_id = {acct}
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

-- Hourly profile per calendar month: {n_months} months of it, which is what
-- makes a year-on-year read possible. August appears twice -- profile_month
-- 2023-08-01 and 2024-08-01 -- so the same hour can be compared against the
-- same hour a year earlier rather than against last month.
INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT {icp_id},
       date_trunc('month', d)::date,
       hour_of_day, is_weekday,
       -- Divided by HOURS OBSERVED, not by days. A local clock hour is not
       -- always an hour long: when New Zealand leaves daylight time the 2am
       -- hour runs twice and that date brings FOUR half hours to the group,
       -- and when it enters daylight time the 2am hour never happens and the
       -- date brings none. COUNT(DISTINCT d) books the doubled hour as a
       -- single day and inflates that hour's average -- 12.5% on the April
       -- weekend rows of the 13-month fixture, which spans both transitions.
       -- COUNT(DISTINCT interval_start) / 2 is the true number of hours behind
       -- the sum, so both transitions normalise themselves.
       ROUND(SUM(kwh) / NULLIF(COUNT(DISTINCT interval_start) / 2.0, 0), 4), 0,
       -- sample_days stays a DAY count on purpose. It answers "how much
       -- evidence is behind this bar", which is a different question from
       -- "what is the rate in this hour". On the spring-forward date the hour
       -- is genuinely absent, and one fewer sample day is the honest record.
       COUNT(DISTINCT d)::smallint
FROM (
    SELECT r.interval_start,
           (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')::smallint AS hour_of_day,
           EXTRACT(isodow FROM (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date) <= 5 AS is_weekday,
           r.kwh
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- Bill explanation, every month against the one before it.
--
-- Same attribution as the two-period fixtures, but sequenced with LAG over
-- the thirteen bills instead of a join between two known ids. Nothing else
-- changes: the drivers are still measured in the schema's order and converted
-- to dollars at the PRIOR period's own average variable rate.
--
-- Because the household term is constant here, delta_from_weather should
-- dominate in both directions -- positive going into winter, negative coming
-- out of it. A fixture where the explained driver is negative half the time is
-- worth having: an explainer that only ever handles increases will show it.
-- ---------------------------------------------------------------------
WITH per_bill AS (
    SELECT b.bill_id, b.period_start, b.period_end, b.period_days,
           b.charges_excl_gst AS amount, b.total_import_kwh AS kwh,
           (SELECT ROUND(AVG(base_load_kwh), 4) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS base_day,
           (SELECT ROUND(SUM(heating_degree_days), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS hdd,
           (SELECT ROUND(AVG(temp_mean_c), 2) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS temp,
           (SELECT COALESCE(SUM(estimated_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS est_intervals,
           (SELECT COALESCE(SUM(expected_intervals - interval_count), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS missing_intervals,
           (SELECT COALESCE(SUM(expected_intervals), 0) FROM kea.mart_daily_usage u
             WHERE u.icp_id = {icp_id} AND u.usage_date BETWEEN b.period_start AND b.period_end) AS expected_intervals
    FROM billing.bill b WHERE b.account_id = {acct}
), seq AS (
    SELECT p.*,
           LAG(p.bill_id)      OVER w AS prior_id,
           LAG(p.period_days)  OVER w AS prior_days,
           LAG(p.amount)       OVER w AS prior_amount,
           LAG(p.kwh)          OVER w AS prior_kwh,
           LAG(p.base_day)     OVER w AS prior_base,
           LAG(p.hdd)          OVER w AS prior_hdd,
           LAG(p.temp)         OVER w AS prior_temp
    FROM per_bill p
    WINDOW w AS (ORDER BY p.period_start)
), rated AS (
    SELECT s.*,
           ROUND((s.prior_amount - {r_daily} * s.prior_days) / NULLIF(s.prior_kwh, 0), 6) AS rate_per_kwh
    FROM seq s WHERE s.prior_id IS NOT NULL
), attributed AS (
    SELECT r.*,
           ROUND(r.amount - r.prior_amount, 2) AS d_amount,
           ROUND((r.period_days - r.prior_days)
                 * (r.prior_kwh / NULLIF(r.prior_days, 0) * r.rate_per_kwh + {r_daily}), 2) AS d_period,
           ROUND({heat_sens} * (r.hdd - r.prior_hdd) * r.rate_per_kwh, 2) AS d_weather,
           ROUND((r.base_day - r.prior_base) * r.period_days * r.rate_per_kwh, 2) AS d_base
    FROM rated r
)
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, prior_bill_id, prior_period_days, prior_total_amount,
     prior_total_kwh, amount_delta, amount_delta_pct, kwh_delta,
     delta_from_period_length, delta_from_weather, delta_from_base_load,
     delta_from_tariff, delta_unexplained, hdd_this_period, hdd_prior_period,
     temp_mean_this, temp_mean_prior, base_load_kwh_day_this, base_load_kwh_day_prior,
     heating_sensitivity_kwh_per_hdd, estimated_read_pct, missing_interval_count,
     is_explainable)
SELECT bill_id, {acct}, {icp_id}, period_start, period_end, period_days,
       amount, kwh, prior_id, prior_days, prior_amount, prior_kwh,
       d_amount,
       ROUND(d_amount / NULLIF(prior_amount, 0) * 100, 2),
       ROUND(kwh - prior_kwh, 4),
       d_period, d_weather, d_base, 0.00,
       ROUND(d_amount - d_period - d_weather - d_base, 2),
       hdd, prior_hdd, temp, prior_temp, base_day, prior_base, {heat_sens},
       ROUND(est_intervals * 100.0 / NULLIF(expected_intervals, 0), 2),
       missing_intervals,
       (ABS(d_period + d_weather + d_base) >= ABS(d_amount) * 0.60
        AND est_intervals = 0 AND missing_intervals = 0)
FROM attributed;

-- The earliest bill has nothing before it, so no driver can be attributed.
INSERT INTO kea.mart_bill_explanation
    (bill_id, account_id, icp_id, period_start, period_end, period_days,
     total_amount, total_kwh, estimated_read_pct, missing_interval_count, is_explainable)
SELECT bill_id, {acct}, {icp_id}, period_start, period_end, period_days,
       charges_excl_gst, COALESCE(total_import_kwh, 0), 0, 0, false
FROM billing.bill WHERE bill_id = {bill_first};

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_{slug}', now(), now(), {n_months}, 'sample-scenario-{slug}', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_span integer; v_days integer; v_rows integer; v_gaps integer;
    v_bills integer; v_months integer; v_bad integer;
    v_short integer; v_long integer;
    v_winter numeric; v_summer numeric;
    v_first numeric; v_last numeric; v_kwh numeric;
BEGIN
    -- 1. THE POINT OF THIS FILE: more than a year of meter data.
    SELECT (MAX(interval_start AT TIME ZONE 'Pacific/Auckland')::date
            - MIN(interval_start AT TIME ZONE 'Pacific/Auckland')::date) + 1
      INTO v_span
      FROM metering.meter_read_hh WHERE icp_id = {icp_id};
    IF v_span < 366 THEN
        RAISE EXCEPTION 'reads span only % days; this fixture exists to carry more than a year', v_span;
    END IF;
    IF v_span <> {n_days} THEN
        RAISE EXCEPTION 'reads span % days, expected {n_days}', v_span;
    END IF;

    -- 2. thirteen bills, each header equal to the sum of its own lines
    SELECT COUNT(*) INTO v_bills FROM billing.bill WHERE account_id = {acct};
    IF v_bills <> {n_months} THEN
        RAISE EXCEPTION 'expected {n_months} bills, found %', v_bills;
    END IF;
    SELECT COUNT(*) INTO v_bad
      FROM billing.bill b
      JOIN (SELECT bill_id, ROUND(SUM(amount), 2) AS s FROM billing.bill_line
             GROUP BY bill_id) l ON l.bill_id = b.bill_id
     WHERE b.account_id = {acct} AND l.s <> b.charges_excl_gst;
    IF v_bad > 0 THEN
        RAISE EXCEPTION '% bill headers disagree with their lines', v_bad;
    END IF;

    -- 3. one daily row per day, and no holes in the middle
    SELECT COUNT(*), (MAX(usage_date) - MIN(usage_date)) + 1
      INTO v_rows, v_days
      FROM kea.mart_daily_usage WHERE icp_id = {icp_id};
    IF v_rows <> {n_days} OR v_days <> v_rows THEN
        RAISE EXCEPTION 'mart_daily_usage has % rows spanning % days, expected {n_days} of each',
                        v_rows, v_days;
    END IF;
    SELECT COUNT(*) INTO v_gaps
      FROM kea.mart_daily_usage
     WHERE icp_id = {icp_id} AND interval_count <> expected_intervals;
    IF v_gaps > 0 THEN
        RAISE EXCEPTION '% days where the reads do not fill the local day', v_gaps;
    END IF;

    -- 4. thirteen calendar months in the hourly profile, so the same month
    --    appears twice and a year-on-year comparison is possible
    SELECT COUNT(DISTINCT profile_month) INTO v_months
      FROM kea.mart_hourly_profile WHERE icp_id = {icp_id};
    IF v_months <> {n_months} THEN
        RAISE EXCEPTION 'hourly profile covers % months, expected {n_months}', v_months;
    END IF;

    -- 5. both DST transitions are inside the window, so the 46- and
    --    50-interval local days the schema warns about are actually present.
    --    If this ever fails, the fixture has stopped exercising them.
    SELECT COUNT(*) FILTER (WHERE expected_intervals = 46),
           COUNT(*) FILTER (WHERE expected_intervals = 50)
      INTO v_short, v_long
      FROM kea.mart_daily_usage WHERE icp_id = {icp_id};
    IF v_short < 1 OR v_long < 1 THEN
        RAISE EXCEPTION 'expected at least one 46-interval and one 50-interval day, found % and %',
                        v_short, v_long;
    END IF;

    -- 6. the seasonal swing must be real, or a year of reads demonstrates
    --    nothing a single month could not
    SELECT ROUND(AVG(import_kwh), 2) INTO v_winter
      FROM kea.mart_daily_usage
     WHERE icp_id = {icp_id} AND usage_date BETWEEN '2024-07-01' AND '2024-07-31';
    SELECT ROUND(AVG(import_kwh), 2) INTO v_summer
      FROM kea.mart_daily_usage
     WHERE icp_id = {icp_id} AND usage_date BETWEEN '2024-01-01' AND '2024-01-31';
    IF v_winter < v_summer * 1.25 THEN
        RAISE EXCEPTION 'winter % kWh/day is not meaningfully above summer % kWh/day',
                        v_winter, v_summer;
    END IF;

    -- 7. on every bill that has a predecessor, the drivers must add up
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_bill_explanation
     WHERE account_id = {acct} AND prior_bill_id IS NOT NULL
       AND ROUND(delta_from_period_length + delta_from_weather + delta_from_base_load
                 + delta_from_tariff + delta_unexplained, 2) <> amount_delta;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'drivers fail to sum to the delta on % bills', v_bad;
    END IF;

    SELECT charges_excl_gst INTO v_first FROM billing.bill WHERE bill_id = {bill_first};
    SELECT charges_excl_gst INTO v_last  FROM billing.bill WHERE bill_id = {bill_last};
    SELECT ROUND(SUM(kwh), 0) INTO v_kwh FROM metering.meter_read_hh WHERE icp_id = {icp_id};

    RAISE NOTICE
        '{slug}: % days of reads (% intervals, % kWh) across % bills; winter % vs summer % kWh/day; % first bill $%, last $%',
        v_span,
        (SELECT COUNT(*) FROM metering.meter_read_hh WHERE icp_id = {icp_id}),
        v_kwh, v_bills, v_winter, v_summer,
        v_short || ' short/' || v_long || ' long DST days;', v_first, v_last;
END $$;

COMMIT;

-- Verification queries:
-- SELECT period_start, period_end, period_days, charges_excl_gst, total_import_kwh
--   FROM billing.bill WHERE account_id = {acct} ORDER BY period_start;
-- -- the same month, a year apart
-- SELECT profile_month, hour_of_day, avg_import_kwh
--   FROM kea.mart_hourly_profile
--  WHERE icp_id = {icp_id} AND is_weekday
--    AND profile_month IN (date '2023-08-01', date '2024-08-01')
--  ORDER BY hour_of_day, profile_month;
-- -- the seasonal curve
-- SELECT date_trunc('month', usage_date)::date AS month,
--        ROUND(AVG(import_kwh), 2) AS kwh_per_day,
--        ROUND(AVG(temp_mean_c), 1) AS temp_c
--   FROM kea.mart_daily_usage WHERE icp_id = {icp_id}
--  GROUP BY 1 ORDER BY 1;
-- -- the two days that are not 48 intervals long
-- SELECT usage_date, interval_count, expected_intervals
--   FROM kea.mart_daily_usage
--  WHERE icp_id = {icp_id} AND expected_intervals <> 48;
'''


# ---------------------------------------------------------------------
# Emit the history fixture. Same two-pass format as the loop above: the
# shared blocks are rendered first, then embedded.
# ---------------------------------------------------------------------
_h = HISTORY
_last_day = (datetime.date.fromisoformat(_h.last_month).replace(day=28)
             + datetime.timedelta(days=4))
_last_day = (_last_day - datetime.timedelta(days=_last_day.day)).isoformat()
_n_months = ((datetime.date.fromisoformat(_h.last_month).year
              - datetime.date.fromisoformat(_h.first_month).year) * 12
             + datetime.date.fromisoformat(_h.last_month).month
             - datetime.date.fromisoformat(_h.first_month).month + 1)
hf = dict(
    title=_h.title, lede=_h.lede, slug=_h.slug,
    bill_first=oid("bill", _h.fx, 1), bill_last=oid("bill", _h.fx, _n_months),
    **ids(_h.fx))
hf.update(
    net_code=_h.net_code, net_name=_h.net_name, region=_h.region, city=_h.city,
    cust_no=_h.cust_no, cust_name=_h.cust_name, icp=_h.icp,
    addr=_h.addr, suburb=_h.suburb,
    plan_code="DEMO_STD_FXTOU_%s" % _h.slug.upper(),
    first_month=_h.first_month, last_month=_h.last_month,
    hist_from=_h.first_month, hist_to=_last_day,
    n_months=_n_months, n_days=days_between(_h.first_month, _last_day),
    daily_kwh="%.2f" % _h.daily_kwh,
    t_mean=T_MEAN, t_amp=T_AMP,
    temp_d=season_temp("d"), temp_sp=season_temp("sp.d"),
    weight=WEIGHT, heat=HEAT, heat_sens=HEAT_SENS,
    loss=LOSS_FACTOR, loss_pct=round(LOSS_FACTOR * 100, 2),
    **{("r_" + k): v for k, v in RATES.items()})

hf["catalogue_src"] = CATALOGUE_SRC.format(**hf)
hf["replica"] = REPLICA_SQL.format(**hf)

_path = outfile(_h.fx, "bill_%s" % _h.slug)
with open(_path, "w") as fh:
    fh.write(HISTORY_TEMPLATE.format(**hf))
print("wrote", _path)


# =====================================================================
# MID-CYCLE FORECAST FIXTURE
#
# Every fixture above seeds bills that have already been issued. Nothing in
# data/sample has ever populated kea.mart_forecast_input or kea.bill_forecast,
# which means Outcome 3 -- "what is my bill going to be?" -- has had no data
# behind it at all.
#
# This is the other half of the product. The billing period is UNFINISHED:
# reads exist up to an as-at date and stop, because a mid-cycle fixture with
# reads for days that have not happened would be seeding the future. Around
# that sit two completed bills that give the forecast something to stand on --
# the same period last year (the comparison anchor) and the most recent mild
# month (where the blended variable rate and the weather-stripped baseline come
# from).
#
# It reuses the same shape, heating response, rates and rating SQL as
# everything else in this file, so the figures stay comparable. What it adds is
# the pair of forecast tables and the arithmetic between them, worked exactly
# as kea-data-platform-design.md 8.5 describes it.
# =====================================================================

FORECAST = S(
    slug="mid_cycle_forecast", fx=9,
    title="MID-CYCLE FORECAST — a bill shock caught before the bill arrives",
    net_code="NTSM", net_name="Network Tasman", region="Tasman", city="Richmond",
    cust_no="219001", cust_name="Mereana Tipene",
    icp="DEMO0000000901AA", addr="8 Waimea Road", suburb="Hope",
    # The comparison anchor: the same calendar period one year earlier.
    ly=("2023-06-01", "2023-06-30"), ly_temp=11.2,
    # The most recent completed bill. Mild, so it is a clean read on the
    # household's own rate and its weather-neutral baseline.
    ap=("2024-04-01", "2024-04-30"), ap_temp=15.4,
    # The period in flight. Colder than last June by nearly five degrees, which
    # is the whole story: nothing in the house changed.
    cur=("2024-06-01", "2024-06-30"), cur_temp=6.4,
    as_at="2024-06-10",
    daily_kwh=9.20,
    # Uncertainty applied to the part of the period that has not happened yet.
    band_pct=0.15,
    # Variance from last year at which the forecast is worth interrupting
    # someone about.
    anom_pct=15,
    model_version="hdd-linear-v2",
    lede="Ten days into a thirty-day winter period, tracking about 28 percent above "
         "the same period last year. The bill has not been issued and the customer "
         "has not asked yet.")


def temp_expr(d, f):
    """Deterministic temperature at date expression `d`: a period mean plus the
    same fixed daily wobble every other fixture uses, so degree days vary day to
    day and a single day is never exactly average."""
    return ("ROUND(((CASE WHEN %s BETWEEN date '%s' AND date '%s' THEN %.1f"
            " WHEN %s BETWEEN date '%s' AND date '%s' THEN %.1f"
            " ELSE %.1f END)::numeric + 2.2 * SIN(EXTRACT(day FROM %s) * 0.7))::numeric, 2)"
            % (d, f["ly_from"], f["ly_to"], f["ly_temp"],
               d, f["ap_from"], f["ap_to"], f["ap_temp"],
               f["cur_temp"], d))


FORECAST_TEMPLATE = r'''-- =====================================================================
-- Kea Energy — mid-cycle forecast seed
-- {title}
-- =====================================================================
--
-- {lede}
--
-- Synthetic fixture. No real customer, address, ICP or meter appears here;
-- every id carries the table it belongs to in its first two digits and this
-- fixture's number ({fx}) in its third -- see "Fixture ids" in data/README.md.
-- The half-hourly reads are generated deterministically by the CTE below
-- rather than copied from a feed.
--
-- WHY THIS FIXTURE EXISTS
-- kea.mart_forecast_input and kea.bill_forecast had no sample data. Every
-- other file in data/sample seeds bills that have already been issued, which
-- exercises the explainer (Outcome 2) and leaves the forecast (Outcome 3)
-- with nothing behind it. This file is the unfinished-period case.
--
-- THE SHAPE OF IT
--   {ly_from} .. {ly_to}   completed bill — the same period LAST YEAR,
--                                the comparison anchor
--   {ap_from} .. {ap_to}   completed bill — the most recent MILD month,
--                                where the blended variable rate and the
--                                weather-stripped baseline come from
--   {cur_from} .. {cur_to}   IN FLIGHT. Reads stop at {as_at}; there is
--                                no bill, because the period has not ended
--
-- Reads deliberately stop at the as-at date. A mid-cycle fixture carrying
-- reads for days that have not happened would be seeding the future, and
-- every "days_elapsed" figure derived from it would be a lie.
--
-- WHAT IT DEMONSTRATES
--   * {n_snap} daily snapshots of the input, one per elapsed day, INSERTed rather
--     than overwritten — as_at_date is in the key, so nothing is lost
--   * hdd_to_date rising while hdd_expected_remaining falls: fact displacing
--     forecast, day by day
--   * the confidence band narrowing along that same curve, because the
--     uncertainty only ever applies to the part that has not happened
--   * baseline_daily_kwh recovered by STRIPPING weather out of history, not
--     by averaging it — the self-check asserts the stripped figure comes back
--     to the {daily_kwh} kWh/day the reads were actually generated from
--   * fixed_charges_period pulled out before the band is applied, which is
--     what keeps "$3 either side of certain" from being priced as uncertain
--
-- WHAT THE EXPLAINER MAY SAY
-- The cause here is weather, which is the one driver the pipeline can measure
-- directly. It still may not name an appliance: data/README.md is explicit
-- that half-hourly interval data cannot identify one, and calls the refusal
-- deliberate. "This June is running about five degrees colder than last June"
-- is measured. "Your heat pump is running more" is a guess.
--
-- NO mart_bill_explanation ROWS. The two completed bills are ten months
-- apart, so a period-over-period decomposition between them would be
-- meaningless. The explainer mart is exercised by the six bill_higher_*
-- fixtures; this one is about the forecast.
--
-- Note {ap_dst}: the day daylight saving ends. That local day is 50 half
-- hours long, not 48, and expected_intervals says so.
--
-- Re-runnable: every write is either scoped-DELETE-then-INSERT or
-- ON CONFLICT DO UPDATE, so running this file twice leaves the same rows.
-- The DO block at the end re-derives the forecast from the seeded data and
-- raises if anything disagrees.
--
-- Run after kea_source_schema.sql and kea_serving_schema.sql.
-- Independent of every other file in data/sample: its own household, its own
-- network, its own plan.

BEGIN;

SET search_path TO public;

-- ---------------------------------------------------------------------
-- Idempotency: remove only this fixture, children before parents.
-- ---------------------------------------------------------------------
DELETE FROM src_metering.interval_reads WHERE icp_identifier = '{icp}';
DELETE FROM metering.meter_read_hh      WHERE icp_id = {icp_id};
DELETE FROM src_billing.invoice_lines   WHERE INV_ID IN ({bill_ly}, {bill_ap});
DELETE FROM src_billing.invoices        WHERE INV_ID IN ({bill_ly}, {bill_ap});
DELETE FROM src_billing.accounts        WHERE ACCT_ID = {acct};
DELETE FROM src_billing.customers       WHERE CUST_ID = {cust};
DELETE FROM src_catalogue.rate_components WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM src_catalogue.tou_windows     WHERE window_id    BETWEEN {k1} AND {k11};
DELETE FROM src_catalogue.price_plans     WHERE plan_id = {plan};
DELETE FROM src_metering.registers        WHERE register_id = {reg};
DELETE FROM src_metering.meters           WHERE meter_id = {meter};
DELETE FROM src_metering.connection_points WHERE icp_id = {icp_id};
DELETE FROM src_metering.networks         WHERE network_id = {net};

-- bill_forecast keys on a GENERATED IDENTITY, so it is cleared by account
-- rather than upserted by id.
DELETE FROM kea.bill_forecast         WHERE account_id = {acct};
DELETE FROM kea.mart_forecast_input   WHERE account_id = {acct};
DELETE FROM kea.mart_hourly_profile   WHERE icp_id = {icp_id};
DELETE FROM kea.mart_daily_usage      WHERE icp_id = {icp_id};
DELETE FROM kea.mart_account_context  WHERE account_id = {acct};
DELETE FROM kea.mart_load_log         WHERE warehouse_run_id = 'sample-scenario-{slug}';
DELETE FROM billing.bill_line WHERE bill_id IN ({bill_ly}, {bill_ap});
DELETE FROM billing.bill      WHERE bill_id IN ({bill_ly}, {bill_ap});
DELETE FROM billing.account   WHERE account_id = {acct};
DELETE FROM catalogue.tariff_component WHERE component_id BETWEEN {m1} AND {m11};
DELETE FROM catalogue.tou_band         WHERE band_id      BETWEEN {k1} AND {k11};
DELETE FROM catalogue.plan             WHERE plan_id = {plan};
DELETE FROM metering.meter_register WHERE register_id = {reg};
DELETE FROM metering.icp            WHERE icp_id = {icp_id};
DELETE FROM metering.property       WHERE property_id = {prop};
DELETE FROM metering.network        WHERE network_id = {net};
DELETE FROM billing.customer        WHERE customer_id = {cust};

-- =====================================================================
-- SOURCE SYSTEMS
-- =====================================================================

INSERT INTO src_billing.customers
    (CUST_ID, CUST_NO, CUST_NAME, EMAIL_ADDR, PHONE_NO,
     ADDR_LINE1, ADDR_SUBURB, ADDR_CITY, ADDR_POSTCODE)
OVERRIDING SYSTEM VALUE
VALUES ({cust}, '{cust_no}', '{cust_name}', NULL, NULL,
        '{addr}', '{suburb}', '{city}', NULL)
ON CONFLICT (CUST_NO) DO UPDATE SET
    CUST_NAME = EXCLUDED.CUST_NAME,
    ADDR_LINE1 = EXCLUDED.ADDR_LINE1,
    ADDR_SUBURB = EXCLUDED.ADDR_SUBURB,
    ADDR_CITY = EXCLUDED.ADDR_CITY;

INSERT INTO src_metering.networks (network_id, network_code, network_name, region)
OVERRIDING SYSTEM VALUE
VALUES ({net}, '{net_code}', '{net_name}', '{region}')
ON CONFLICT (network_code) DO UPDATE SET
    network_name = EXCLUDED.network_name, region = EXCLUDED.region;

INSERT INTO src_metering.connection_points
    (icp_id, icp_identifier, network_id, address_full, suburb, city,
     postcode, climate_region, connection_date)
OVERRIDING SYSTEM VALUE
VALUES ({icp_id}, '{icp}', {net}, '{addr}, {suburb}',
        '{suburb}', '{city}', NULL, '{region}', '2019-01-01')
ON CONFLICT (icp_identifier) DO UPDATE SET
    network_id = EXCLUDED.network_id,
    address_full = EXCLUDED.address_full,
    climate_region = EXCLUDED.climate_region;

INSERT INTO src_metering.meters (meter_id, icp_id, serial_number, is_smart, installed_on)
OVERRIDING SYSTEM VALUE
VALUES ({meter}, {icp_id}, 'DEMO-METER-{meter}', true, '2019-01-01')
ON CONFLICT (meter_id) DO UPDATE SET
    icp_id = EXCLUDED.icp_id, serial_number = EXCLUDED.serial_number;

INSERT INTO src_metering.registers
    (register_id, meter_id, register_code, description, is_controlled, flow_direction)
OVERRIDING SYSTEM VALUE
VALUES ({reg}, {meter}, 'UN', 'Grid import', false, 'import')
ON CONFLICT (register_id) DO UPDATE SET
    meter_id = EXCLUDED.meter_id, register_code = EXCLUDED.register_code;

-- ---------------------------------------------------------------------
-- Half-hourly reads for three windows: the two completed periods and the
-- elapsed part of the period in flight.
--
-- kwh = daily_total * shape(hh) / sum(shape over day)      the household
--     + heating(hh) * heating_degree_days(day)             weather response
--
-- There is no scenario_extra term. Nothing about this household changes
-- across the three windows -- daily_total is {daily_kwh} throughout -- so the
-- entire rise in the period in flight is weather, and the forecast has a
-- single legible cause. That also makes the baseline recoverable exactly:
-- strip {heat_sens} kWh per degree day back out of any historical day and
-- {daily_kwh} is what must remain.
-- ---------------------------------------------------------------------
WITH half_hours AS (
    -- Anchored to NZ LOCAL midnight via AT TIME ZONE rather than a fixed +12
    -- literal: +12 is NZST, and April is partly NZDT (+13).
    SELECT generate_series(
               (date '{ly_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '{ly_to}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes') AS interval_start
    UNION ALL
    SELECT generate_series(
               (date '{ap_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '{ap_to}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes')
    UNION ALL
    -- stops at the as-at date, not at period end
    SELECT generate_series(
               (date '{cur_from}')::timestamp AT TIME ZONE 'Pacific/Auckland',
               (date '{as_at}' + 1)::timestamp AT TIME ZONE 'Pacific/Auckland'
                   - interval '30 minutes',
               interval '30 minutes')
), tagged AS (
    SELECT interval_start,
           (interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour   FROM interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM half_hours
), weathered AS (
    SELECT t.*, {temp_d} AS temp_c FROM tagged t
), hdd AS (
    SELECT w.*, GREATEST(16.0 - temp_c, 0)::numeric AS hdd_day FROM weathered w
), shaped AS (
    SELECT h.*,
           {weight}::numeric AS w,
           {daily_kwh}::numeric AS daily_kwh,
           {heat}::numeric * hdd_day AS heat_kwh
    FROM hdd h
), normed AS (
    SELECT s.*, SUM(w) OVER (PARTITION BY d) AS w_day FROM shaped s
)
INSERT INTO src_metering.interval_reads
    (icp_identifier, register_code, interval_start, kwh, read_status, version_no, source_file)
SELECT '{icp}', 'UN', interval_start,
       ROUND(daily_kwh * w / w_day + heat_kwh, 4),
       'A', 1, 'sample-scenario-{slug}.csv'
FROM normed;

{catalogue_src}

INSERT INTO src_billing.invoices
    (INV_ID, INV_NO, ACCT_ID, PLAN_CODE, ISSUE_DT, PERIOD_FROM, PERIOD_TO,
     OPEN_BAL, PAYMENTS_APPL, BAL_BROUGHT_FWD, CHARGES_EXGST, GST_RATE,
     GST_AMT, TOTAL_AMT, DUE_DT, LATE_FEE, TOTAL_IF_LATE, PPD_AMT,
     TOTAL_IMPORT_KWH, TOTAL_EXPORT_KWH, EXPORT_CREDIT, INV_STATUS)
OVERRIDING SYSTEM VALUE
VALUES
    ({bill_ly}, 'DEMO-INV-{bill_ly}', {acct}, '{plan_code}', '{ly_issue}', '{ly_from}', '{ly_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{ly_due}', 0, NULL, 0, 0, 0, 0, 'ISSUED'),
    ({bill_ap}, 'DEMO-INV-{bill_ap}', {acct}, '{plan_code}', '{ap_issue}', '{ap_from}', '{ap_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{ap_due}', 0, NULL, 0, 0, 0, 0, 'ISSUED')
ON CONFLICT (INV_NO) DO UPDATE SET
    ACCT_ID = EXCLUDED.ACCT_ID, PERIOD_FROM = EXCLUDED.PERIOD_FROM,
    PERIOD_TO = EXCLUDED.PERIOD_TO;

-- =====================================================================
-- SERVING REPLICA
-- =====================================================================

{replica}

INSERT INTO billing.bill
    (bill_id, account_id, invoice_number, plan_id, issue_date, period_start, period_end,
     opening_balance, payments_applied, balance_brought_fwd, charges_excl_gst,
     gst_rate, gst_amount, total_charges, due_date, late_payment_fee,
     total_if_paid_late, prompt_payment_discount, total_import_kwh,
     total_export_kwh, export_credit, status)
OVERRIDING SYSTEM VALUE
VALUES
    ({bill_ly}, {acct}, 'DEMO-INV-{bill_ly}', {plan}, '{ly_issue}', '{ly_from}', '{ly_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{ly_due}', 0, NULL, 0, 0, 0, 0, 'issued'),
    ({bill_ap}, {acct}, 'DEMO-INV-{bill_ap}', {plan}, '{ap_issue}', '{ap_from}', '{ap_to}',
     0, 0, 0, 0, 0.00, 0.00, 0, '{ap_due}', 0, NULL, 0, 0, 0, 0, 'issued')
ON CONFLICT (invoice_number) DO UPDATE SET
    period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end;

-- ---------------------------------------------------------------------
-- Bill lines, RATED from the reads. Identical machinery to the bill_higher_*
-- fixtures, so the blended variable rate this forecast leans on is the same
-- rate those files charge.
-- ---------------------------------------------------------------------
WITH banded AS (
    SELECT b.bill_id, b.period_start, b.period_end,
           (b.period_end - b.period_start) + 1 AS days,
           r.kwh,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr
    FROM billing.bill b
    JOIN metering.meter_read_hh r
      ON r.icp_id = {icp_id}
     AND (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date
         BETWEEN b.period_start AND b.period_end
    WHERE b.bill_id IN ({bill_ly}, {bill_ap})
), totals AS (
    SELECT bill_id, days,
           ROUND(SUM(kwh), 2) AS total_kwh,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21.5)), 2) AS net_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 7 AND hr < 11) OR (hr >= 17 AND hr < 21)), 2)   AS ene_peak,
           ROUND(SUM(kwh) FILTER (WHERE (hr >= 11 AND hr < 17) OR (hr >= 21 AND hr < 23)), 2)  AS ene_shoulder
    FROM banded GROUP BY bill_id, days
), lines AS (
    SELECT bill_id, {m1} AS component_id, 'network'::common.charge_group AS cg, 'Peak Usage' AS label,
           'peak' AS band, net_peak AS qty, 'per_kwh'::common.rate_basis AS unit, {r_net_peak}::numeric AS rate,
           NULL::numeric AS loss, ROUND(net_peak * {r_net_peak}, 2) AS amount, 10 AS sort_order FROM totals
    UNION ALL SELECT bill_id, {m2}, 'network', 'Off Peak Usage', 'off_peak',
           total_kwh - net_peak, 'per_kwh', {r_net_offpeak},
           NULL, ROUND((total_kwh - net_peak) * {r_net_offpeak}, 2), 20 FROM totals
    UNION ALL SELECT bill_id, {m3}, 'network', '{net_code} + Daily charges', NULL,
           days::numeric, 'per_day', {r_daily},
           NULL, ROUND(days * {r_daily}, 2), 30 FROM totals
    UNION ALL SELECT bill_id, {m4}, 'network', 'Transmission Charges', NULL,
           total_kwh, 'per_kwh', {r_transmission},
           NULL, ROUND(total_kwh * {r_transmission}, 2), 40 FROM totals
    UNION ALL SELECT bill_id, {m5}, 'energy', 'Peak Usage', 'peak',
           ene_peak, 'per_kwh', {r_ene_peak}, ROUND(ene_peak * {loss}, 2),
           ROUND((ene_peak + ROUND(ene_peak * {loss}, 2)) * {r_ene_peak}, 2), 50 FROM totals
    UNION ALL SELECT bill_id, {m7}, 'energy', 'Shoulder Usage', 'shoulder',
           ene_shoulder, 'per_kwh', {r_ene_shoulder}, ROUND(ene_shoulder * {loss}, 2),
           ROUND((ene_shoulder + ROUND(ene_shoulder * {loss}, 2)) * {r_ene_shoulder}, 2), 60 FROM totals
    UNION ALL SELECT bill_id, {m6}, 'energy', 'Off Peak Usage', 'off_peak',
           total_kwh - ene_peak - ene_shoulder, 'per_kwh', {r_ene_offpeak},
           ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2),
           ROUND(((total_kwh - ene_peak - ene_shoulder)
                  + ROUND((total_kwh - ene_peak - ene_shoulder) * {loss}, 2)) * {r_ene_offpeak}, 2), 70 FROM totals
    UNION ALL SELECT bill_id, {m11}, 'other', 'ELECTRICITY AUTHORITY Levy', NULL,
           total_kwh, 'per_kwh', {r_levy},
           NULL, ROUND(total_kwh * {r_levy}, 2), 80 FROM totals
)
INSERT INTO billing.bill_line
    (bill_id, component_id, charge_group, direction, line_label, band_name,
     quantity, unit, rate, loss_kwh, amount, sort_order)
SELECT bill_id, component_id, cg, 'import', label, band, qty, unit, rate, loss, amount, sort_order
FROM lines;

INSERT INTO src_billing.invoice_lines
    (INV_ID, CHG_GROUP, FLOW_DIR, LINE_DESC, TOU_BAND, QTY, UOM, UNIT_RATE, LOSS_QTY, LINE_AMT, SORT_SEQ)
SELECT bill_id,
       CASE charge_group WHEN 'network' THEN 'NET' WHEN 'energy' THEN 'ENE' ELSE 'OTH' END,
       'IMPORT', line_label, UPPER(band_name), quantity,
       CASE unit WHEN 'per_day' THEN 'DAY' ELSE 'KWH' END,
       rate, loss_kwh, amount, sort_order
FROM billing.bill_line WHERE bill_id IN ({bill_ly}, {bill_ap});

UPDATE billing.bill AS b
SET charges_excl_gst = t.charges,
    gst_amount       = ROUND(t.charges * b.gst_rate, 2),
    total_charges    = ROUND(t.charges * (1 + b.gst_rate), 2),
    total_import_kwh = t.import_kwh
FROM (SELECT bill_id, ROUND(SUM(amount), 2) AS charges,
             SUM(quantity) FILTER (WHERE charge_group = 'energy' AND unit = 'per_kwh') AS import_kwh
      FROM billing.bill_line WHERE bill_id IN ({bill_ly}, {bill_ap}) GROUP BY bill_id) AS t
WHERE b.bill_id = t.bill_id;

UPDATE src_billing.invoices AS i
SET CHARGES_EXGST = t.charges,
    GST_AMT       = ROUND(t.charges * i.GST_RATE, 2),
    TOTAL_AMT     = ROUND(t.charges * (1 + i.GST_RATE), 2),
    TOTAL_IMPORT_KWH = t.import_kwh
FROM (SELECT INV_ID, ROUND(SUM(LINE_AMT), 2) AS charges,
             SUM(CASE WHEN CHG_GROUP = 'ENE' AND UOM = 'KWH' THEN QTY ELSE 0 END) AS import_kwh
      FROM src_billing.invoice_lines WHERE INV_ID IN ({bill_ly}, {bill_ap}) GROUP BY INV_ID) AS t
WHERE i.INV_ID = t.INV_ID;

-- =====================================================================
-- MARTS
-- =====================================================================

-- days_into_period is {n_snap}, not {cur_days}: the period is in flight, and this is
-- the column the agent checks before it says anything about "this bill".
INSERT INTO kea.mart_account_context
    (account_id, account_number, customer_id, customer_number, customer_name,
     email, icp_id, icp_number, address_display, climate_region, network_code,
     network_name, has_solar, has_controlled_load, plan_id, plan_code, plan_name,
     user_type, is_tou, daily_fixed_charge, payment_method, billing_cycle_day,
     current_period_start, current_period_end, days_into_period, estimated_read_pct,
     last_read_at, is_quarantined)
VALUES
    ({acct}, 'DEMO-ACCT-{acct}', {cust}, '{cust_no}', '{cust_name}', NULL,
     {icp_id}, '{icp}', '{addr}, {suburb}', '{region}', '{net_code}', '{net_name}',
     false, false, {plan}, '{plan_code}', 'Demo Standard Fixed TOU', 'standard', true,
     {r_daily}, 'direct_debit', 28, '{cur_from}', '{cur_to}', {n_snap},
     0, timestamptz '{as_at} 23:30:00+12', false)
ON CONFLICT (account_id) DO UPDATE SET
    current_period_start = EXCLUDED.current_period_start,
    current_period_end   = EXCLUDED.current_period_end,
    days_into_period     = EXCLUDED.days_into_period,
    last_read_at         = EXCLUDED.last_read_at;

-- Daily usage over all three windows. The spine covers the two billed periods
-- plus the ELAPSED days of the period in flight -- days {as_at_next} to {cur_to}
-- get no row, because the meter has not reported them and an empty row would
-- read as a gap rather than as the future.
INSERT INTO kea.mart_daily_usage
    (icp_id, usage_date, import_kwh, export_kwh, controlled_kwh, uncontrolled_kwh,
     base_load_kwh, peak_kwh, off_peak_kwh, interval_count, expected_intervals,
     estimated_count, temp_mean_c, heating_degree_days, is_weekend)
SELECT {icp_id},
       sp.d,
       COALESCE(ROUND(SUM(x.kwh), 4), 0), 0, 0, COALESCE(ROUND(SUM(x.kwh), 4), 0),
       ROUND(MIN(x.kwh) * 48, 4),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE (x.hr >= 7 AND x.hr < 11) OR (x.hr >= 17 AND x.hr < 21)), 4), 0),
       COALESCE(ROUND(SUM(x.kwh) FILTER (WHERE x.hr < 7 OR x.hr >= 23), 4), 0),
       COUNT(x.kwh)::smallint,
       (EXTRACT(epoch FROM ((sp.d + 1)::timestamp AT TIME ZONE 'Pacific/Auckland')
                          - (sp.d::timestamp AT TIME ZONE 'Pacific/Auckland')) / 1800)::smallint,
       COUNT(*) FILTER (WHERE x.quality = 'estimated')::smallint,
       {temp_sp},
       ROUND(GREATEST(16.0 - {temp_sp}, 0), 2),
       EXTRACT(isodow FROM sp.d) >= 6
FROM (
    SELECT gs::date AS d
    FROM billing.bill b
    CROSS JOIN LATERAL generate_series(b.period_start, b.period_end, interval '1 day') gs
    WHERE b.bill_id IN ({bill_ly}, {bill_ap})
    UNION ALL
    SELECT gs::date
    FROM generate_series(date '{cur_from}', date '{as_at}', interval '1 day') gs
) sp
LEFT JOIN (
    SELECT (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')
             + EXTRACT(minute FROM r.interval_start AT TIME ZONE 'Pacific/Auckland') / 60.0 AS hr,
           r.kwh, r.quality
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) x ON x.d = sp.d
GROUP BY sp.d
ON CONFLICT (icp_id, usage_date) DO UPDATE SET
    import_kwh = EXCLUDED.import_kwh, base_load_kwh = EXCLUDED.base_load_kwh,
    peak_kwh = EXCLUDED.peak_kwh, off_peak_kwh = EXCLUDED.off_peak_kwh,
    interval_count = EXCLUDED.interval_count, estimated_count = EXCLUDED.estimated_count,
    expected_intervals = EXCLUDED.expected_intervals,
    temp_mean_c = EXCLUDED.temp_mean_c, heating_degree_days = EXCLUDED.heating_degree_days;

INSERT INTO kea.mart_hourly_profile
    (icp_id, profile_month, hour_of_day, is_weekday, avg_import_kwh, avg_export_kwh, sample_days)
SELECT {icp_id},
       date_trunc('month', d)::date,
       hour_of_day, is_weekday,
       -- Divided by HOURS OBSERVED, not by days. A local clock hour is not
       -- always an hour long: when New Zealand leaves daylight time the 2am
       -- hour runs twice and that date brings FOUR half hours to the group,
       -- and when it enters daylight time the 2am hour never happens and the
       -- date brings none. COUNT(DISTINCT d) books the doubled hour as a
       -- single day and inflates that hour's average -- 12.5% on the April
       -- weekend rows of the 13-month fixture, which spans both transitions.
       -- COUNT(DISTINCT interval_start) / 2 is the true number of hours behind
       -- the sum, so both transitions normalise themselves.
       ROUND(SUM(kwh) / NULLIF(COUNT(DISTINCT interval_start) / 2.0, 0), 4), 0,
       -- sample_days stays a DAY count on purpose. It answers "how much
       -- evidence is behind this bar", which is a different question from
       -- "what is the rate in this hour". On the spring-forward date the hour
       -- is genuinely absent, and one fewer sample day is the honest record.
       COUNT(DISTINCT d)::smallint
FROM (
    SELECT r.interval_start,
           (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date AS d,
           EXTRACT(hour FROM r.interval_start AT TIME ZONE 'Pacific/Auckland')::smallint AS hour_of_day,
           EXTRACT(isodow FROM (r.interval_start AT TIME ZONE 'Pacific/Auckland')::date) <= 5 AS is_weekday,
           r.kwh
    FROM metering.meter_read_hh r WHERE r.icp_id = {icp_id}
) y
GROUP BY 2, 3, 4
ON CONFLICT (icp_id, profile_month, hour_of_day, is_weekday) DO UPDATE SET
    avg_import_kwh = EXCLUDED.avg_import_kwh, sample_days = EXCLUDED.sample_days;

-- ---------------------------------------------------------------------
-- FORECAST INPUT — one row per elapsed day, the nightly pipeline's output.
--
-- Every figure is DERIVED from what is already seeded, so retuning the reads
-- carries straight through:
--
--   kwh_to_date          summed from mart_daily_usage up to as_at_date
--   hdd_to_date          likewise -- FACT, the meter and the weather station
--   hdd_expected_remaining  the same temperature model run over the days that
--                        have NOT happened. In production this is the NIWA
--                        forecast, and it carries a forecast's error; that
--                        error is most of why bill_forecast stores a band
--
-- The weather forecast here is DELIBERATELY PERFECT -- the remaining days are
-- run through the same formula the reads were built from. Two consequences a
-- reader should not mistake for a claim about real forecasts:
--
--   * hdd_to_date + hdd_expected_remaining is exactly constant across the
--     snapshots, which is a strong invariant to test against and is not
--     something a real NIWA feed would give you
--   * projected_kwh barely moves, because the model IS the process that
--     generated the data. In production the point estimate wanders and
--     converges; here only the BAND narrows
--
-- That is the trade: one cause of narrowing instead of two, so a fixture that
-- stops narrowing has exactly one thing wrong with it.
--   baseline_daily_kwh   history with weather STRIPPED OUT, not averaged.
--                        Averaging a winter month and then adding the heating
--                        term back counts weather twice
--   fixed_charges_period daily rate x the WHOLE period, known on day one
--   cost_to_date         fixed accrued so far + kWh so far at the blended rate
-- ---------------------------------------------------------------------
WITH asat AS (
    SELECT gs::date AS as_at
    FROM generate_series(date '{cur_from}', date '{as_at}', interval '1 day') gs
), rate AS (
    SELECT ROUND((b.charges_excl_gst - {r_daily} * b.period_days)
                 / NULLIF(b.total_import_kwh, 0), 6) AS rate_per_kwh
    FROM billing.bill b WHERE b.bill_id = {bill_ap}
), baseline AS (
    SELECT ROUND(AVG(u.import_kwh - u.heating_degree_days * {heat_sens}), 4) AS baseline_daily_kwh
    FROM kea.mart_daily_usage u
    WHERE u.icp_id = {icp_id}
      AND (u.usage_date BETWEEN date '{ly_from}' AND date '{ly_to}'
        OR u.usage_date BETWEEN date '{ap_from}' AND date '{ap_to}')
), lastyear AS (
    SELECT b.total_import_kwh AS kwh, b.charges_excl_gst AS amount
    FROM billing.bill b WHERE b.bill_id = {bill_ly}
), todate AS (
    SELECT a.as_at,
           (a.as_at - date '{cur_from}' + 1)::smallint AS days_elapsed,
           ROUND(SUM(u.import_kwh), 4) AS kwh_to_date,
           ROUND(SUM(u.heating_degree_days), 2) AS hdd_to_date
    FROM asat a
    JOIN kea.mart_daily_usage u
      ON u.icp_id = {icp_id}
     AND u.usage_date BETWEEN date '{cur_from}' AND a.as_at
    GROUP BY a.as_at
), future AS (
    SELECT a.as_at,
           ROUND(COALESCE(SUM(GREATEST(16.0 - {temp_g}, 0)), 0), 2) AS hdd_expected_remaining
    FROM asat a
    LEFT JOIN LATERAL generate_series(a.as_at + 1, date '{cur_to}', interval '1 day') AS g(d)
           ON true
    GROUP BY a.as_at
)
INSERT INTO kea.mart_forecast_input
    (account_id, period_start, as_at_date, days_elapsed, days_total,
     kwh_to_date, cost_to_date, baseline_daily_kwh,
     same_period_last_year_kwh, same_period_last_year_amount,
     hdd_to_date, hdd_expected_remaining, heating_sensitivity_kwh_per_hdd,
     fixed_charges_period)
SELECT {acct}, date '{cur_from}', t.as_at,
       t.days_elapsed, {cur_days},
       t.kwh_to_date,
       ROUND({r_daily} * t.days_elapsed + t.kwh_to_date * r.rate_per_kwh, 2),
       bl.baseline_daily_kwh,
       ly.kwh, ly.amount,
       t.hdd_to_date, f.hdd_expected_remaining, {heat_sens},
       ROUND({r_daily} * {cur_days}, 2)
FROM todate t
JOIN future f ON f.as_at = t.as_at
CROSS JOIN rate r
CROSS JOIN baseline bl
CROSS JOIN lastyear ly
ON CONFLICT (account_id, period_start, as_at_date) DO UPDATE SET
    days_elapsed = EXCLUDED.days_elapsed,
    kwh_to_date = EXCLUDED.kwh_to_date, cost_to_date = EXCLUDED.cost_to_date,
    baseline_daily_kwh = EXCLUDED.baseline_daily_kwh,
    hdd_to_date = EXCLUDED.hdd_to_date,
    hdd_expected_remaining = EXCLUDED.hdd_expected_remaining,
    fixed_charges_period = EXCLUDED.fixed_charges_period;

-- ---------------------------------------------------------------------
-- FORECAST OUTPUT — the model's row, one per input row.
--
-- Written by a DIFFERENT job in production (kea_forecast_refresh, not
-- kea_daily_pipeline), which is why it is a separate table. It reads nothing
-- but mart_forecast_input.
--
--   rate           re-derived from cost_to_date and fixed_charges_period,
--                  no tariff join -- the self-check asserts it comes back to
--                  the rate the April bill was charged at
--   projected_kwh  what has happened + remaining baseline + remaining weather,
--                  NOT kwh_to_date / days_elapsed x days_total. Straight
--                  extrapolation assumes the rest of the month is as cold as
--                  the part already measured
--   the band       {band_pct_txt} percent, applied ONLY to the kWh that has not
--                  happened. The metered kWh is fact and the daily charge was
--                  fixed on day one; pricing either as uncertain would widen
--                  the band by an amount that is pure fiction
-- ---------------------------------------------------------------------
WITH i AS (
    SELECT * FROM kea.mart_forecast_input
     WHERE account_id = {acct} AND period_start = date '{cur_from}'
), step1 AS (
    SELECT i.*,
           ROUND(i.fixed_charges_period * i.days_elapsed / i.days_total, 2) AS fixed_so_far,
           ROUND((i.days_total - i.days_elapsed) * i.baseline_daily_kwh, 4) AS rem_base_kwh,
           ROUND(i.hdd_expected_remaining * i.heating_sensitivity_kwh_per_hdd, 4) AS rem_heat_kwh
    FROM i
), step2 AS (
    SELECT s.*,
           ROUND((s.cost_to_date - s.fixed_so_far) / NULLIF(s.kwh_to_date, 0), 6) AS rate_per_kwh
    FROM step1 s
), step3 AS (
    SELECT s.*,
           ROUND(s.kwh_to_date + s.rem_base_kwh + s.rem_heat_kwh, 4) AS projected_kwh,
           ROUND((s.rem_base_kwh + s.rem_heat_kwh) * s.rate_per_kwh, 2) AS rem_cost
    FROM step2 s
), step4 AS (
    SELECT s.*,
           ROUND(s.fixed_charges_period + s.projected_kwh * s.rate_per_kwh, 2) AS projected_amount,
           ROUND(s.rem_cost * {band_pct}, 2) AS half_band
    FROM step3 s
)
INSERT INTO kea.bill_forecast
    (account_id, period_start, period_end, forecast_date, days_elapsed,
     kwh_to_date, projected_kwh, projected_amount, projected_low, projected_high,
     baseline_amount, variance_pct, is_anomalous, model_version)
SELECT {acct}, date '{cur_from}', date '{cur_to}', as_at_date, days_elapsed,
       kwh_to_date, projected_kwh, projected_amount,
       projected_amount - half_band,
       projected_amount + half_band,
       same_period_last_year_amount,
       ROUND((projected_amount - same_period_last_year_amount)
             / NULLIF(same_period_last_year_amount, 0) * 100, 2),
       ABS(ROUND((projected_amount - same_period_last_year_amount)
                 / NULLIF(same_period_last_year_amount, 0) * 100, 2)) >= {anom_pct},
       '{model_version}'
FROM step4
ON CONFLICT (account_id, period_start, forecast_date) DO UPDATE SET
    projected_kwh = EXCLUDED.projected_kwh,
    projected_amount = EXCLUDED.projected_amount,
    projected_low = EXCLUDED.projected_low,
    projected_high = EXCLUDED.projected_high,
    variance_pct = EXCLUDED.variance_pct,
    is_anomalous = EXCLUDED.is_anomalous,
    model_version = EXCLUDED.model_version;

INSERT INTO kea.mart_load_log
    (mart_name, started_at, completed_at, row_count, warehouse_run_id, status)
VALUES ('sample_scenario_{slug}', now(), now(), {n_snap}, 'sample-scenario-{slug}', 'succeeded');

-- =====================================================================
-- SELF-CHECK — re-derives the fixture and fails the transaction on drift
-- =====================================================================
DO $$
DECLARE
    v_lines numeric; v_ly numeric; v_ap_rate numeric;
    v_base numeric; v_n integer; v_bad integer;
    v_span_min numeric; v_span_max numeric;
    v_w_first numeric; v_w_last numeric;
    v_amt numeric; v_var numeric; v_anom boolean;
    v_kwh numeric; v_true_kwh numeric;
BEGIN
    -- 1. both bills must equal the sum of their own lines
    SELECT COUNT(*) INTO v_bad
      FROM billing.bill b
      JOIN (SELECT bill_id, ROUND(SUM(amount), 2) AS s FROM billing.bill_line
             WHERE bill_id IN ({bill_ly}, {bill_ap}) GROUP BY bill_id) l
        ON l.bill_id = b.bill_id
     WHERE b.charges_excl_gst <> l.s;
    IF v_bad > 0 THEN
        RAISE EXCEPTION '% bill(s) do not match their own lines', v_bad;
    END IF;

    -- 2. the weather-stripped baseline must recover the kWh/day the reads
    --    were generated from. This is the assertion that would catch someone
    --    "simplifying" baseline_daily_kwh into a plain historical average.
    SELECT DISTINCT baseline_daily_kwh INTO v_base
      FROM kea.mart_forecast_input WHERE account_id = {acct};
    IF ABS(v_base - {daily_kwh}) > 0.05 THEN
        RAISE EXCEPTION
            'weather-stripped baseline came back as % but the reads were built from {daily_kwh}',
            v_base;
    END IF;

    -- 3. one snapshot per elapsed day, and one forecast per snapshot
    SELECT COUNT(*) INTO v_n FROM kea.mart_forecast_input WHERE account_id = {acct};
    IF v_n <> {n_snap} THEN
        RAISE EXCEPTION 'expected {n_snap} forecast input rows, found %', v_n;
    END IF;
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_forecast_input i
      FULL JOIN kea.bill_forecast f
        ON f.account_id = i.account_id AND f.period_start = i.period_start
       AND f.forecast_date = i.as_at_date
     WHERE i.account_id IS NULL OR f.account_id IS NULL;
    IF v_bad > 0 THEN
        -- No FK enforces this pairing: mart_forecast_input is rebuildable and
        -- must not be able to cascade into forecast history. The join is the
        -- job's responsibility, so the fixture checks it.
        RAISE EXCEPTION '% forecast row(s) have no matching input row, or vice versa', v_bad;
    END IF;

    -- 4. fact and forecast must PARTITION the period: hdd_to_date +
    --    hdd_expected_remaining is the same total on every snapshot
    SELECT MIN(hdd_to_date + hdd_expected_remaining),
           MAX(hdd_to_date + hdd_expected_remaining)
      INTO v_span_min, v_span_max
      FROM kea.mart_forecast_input WHERE account_id = {acct};
    IF v_span_max - v_span_min > 0.05 THEN
        RAISE EXCEPTION
            'hdd_to_date + hdd_expected_remaining drifts from % to % across the snapshots',
            v_span_min, v_span_max;
    END IF;

    -- 5. the re-derived rate must come back to what the April bill charged
    SELECT ROUND((charges_excl_gst - {r_daily} * period_days) / total_import_kwh, 4)
      INTO v_ap_rate FROM billing.bill WHERE bill_id = {bill_ap};
    SELECT COUNT(*) INTO v_bad
      FROM kea.mart_forecast_input
     WHERE account_id = {acct}
       AND ABS(ROUND((cost_to_date - ROUND(fixed_charges_period * days_elapsed / days_total, 2))
                     / kwh_to_date, 4) - v_ap_rate) > 0.001;
    IF v_bad > 0 THEN
        RAISE EXCEPTION
            '% snapshot(s) re-derive a variable rate that is not the billed rate of %',
            v_bad, v_ap_rate;
    END IF;

    -- 6. the band must NARROW as the period fills in
    SELECT projected_high - projected_low INTO v_w_first
      FROM kea.bill_forecast WHERE account_id = {acct} ORDER BY forecast_date LIMIT 1;
    SELECT projected_high - projected_low INTO v_w_last
      FROM kea.bill_forecast WHERE account_id = {acct} ORDER BY forecast_date DESC LIMIT 1;
    IF v_w_last >= v_w_first THEN
        RAISE EXCEPTION
            'the confidence band did not narrow: % wide on day 1, % wide on day {n_snap}',
            v_w_first, v_w_last;
    END IF;
    SELECT COUNT(*) INTO v_bad FROM (
        SELECT projected_high - projected_low AS w,
               LAG(projected_high - projected_low) OVER (ORDER BY forecast_date) AS prev
          FROM kea.bill_forecast WHERE account_id = {acct}) z
     WHERE prev IS NOT NULL AND w > prev;
    IF v_bad > 0 THEN
        RAISE EXCEPTION 'the band widened on % day(s) instead of narrowing', v_bad;
    END IF;

    -- 7. the latest forecast must actually be the bill shock this fixture claims
    SELECT projected_amount, variance_pct, is_anomalous, projected_kwh
      INTO v_amt, v_var, v_anom, v_kwh
      FROM kea.bill_forecast WHERE account_id = {acct}
     ORDER BY forecast_date DESC LIMIT 1;
    IF NOT v_anom OR v_var < {anom_pct} THEN
        RAISE EXCEPTION
            'expected an anomalous forecast above {anom_pct} percent, got % percent (anomalous %)',
            v_var, v_anom;
    END IF;

    -- 8. and it must be close to what the whole period will actually come to,
    --    computed straight from the generative model over all {cur_days} days
    SELECT ROUND({daily_kwh} * {cur_days}
                 + {heat_sens} * SUM(GREATEST(16.0 - {temp_v}, 0)), 4)
      INTO v_true_kwh
      FROM generate_series(date '{cur_from}', date '{cur_to}', interval '1 day') AS v(d);
    IF ABS(v_kwh - v_true_kwh) / v_true_kwh > 0.02 THEN
        RAISE EXCEPTION
            'day-{n_snap} projection of % kWh is more than 2 percent off the period total of % kWh',
            v_kwh, v_true_kwh;
    END IF;

    SELECT charges_excl_gst INTO v_ly FROM billing.bill WHERE bill_id = {bill_ly};
    RAISE NOTICE
        '{slug}: last June $%, projected $% (% percent, anomalous %); band $% wide on day 1, $% on day {n_snap}; baseline % kWh/day',
        v_ly, v_amt, v_var, v_anom, v_w_first, v_w_last, v_base;
END $$;

COMMIT;

-- Verification queries:
-- -- the snapshot series: fact displacing forecast, day by day
-- SELECT as_at_date, days_elapsed, kwh_to_date, hdd_to_date, hdd_expected_remaining
--   FROM kea.mart_forecast_input WHERE account_id = {acct} ORDER BY as_at_date;
-- -- and the band narrowing along it
-- SELECT forecast_date, projected_kwh, projected_low, projected_amount, projected_high,
--        projected_high - projected_low AS band, variance_pct, is_anomalous
--   FROM kea.bill_forecast WHERE account_id = {acct} ORDER BY forecast_date;
-- -- what the agent would actually read
-- SELECT projected_low, projected_amount, projected_high, baseline_amount, variance_pct
--   FROM kea.bill_forecast WHERE account_id = {acct}
--  ORDER BY forecast_date DESC LIMIT 1;
'''


# ---------------------------------------------------------------------
# Emit the forecast fixture. Same two-pass format as the blocks above: the
# shared source/replica SQL is rendered first, then embedded.
# ---------------------------------------------------------------------
_f = FORECAST
_ly_issue, _ly_due = issue_due(_f.ly[1])
_ap_issue, _ap_due = issue_due(_f.ap[1])
_as_at = datetime.date.fromisoformat(_f.as_at)

# NZDT ends on the first Sunday in April: that local day is 50 half hours long
# and expected_intervals has to say so rather than assume 48.
_apr = datetime.date(_as_at.year, 4, 1)
_dst_end = _apr + datetime.timedelta(days=(6 - _apr.weekday()) % 7)

ff = dict(
    title=_f.title, lede=_f.lede, slug=_f.slug,
    bill_ly=oid("bill", _f.fx, 1), bill_ap=oid("bill", _f.fx, 2),
    **ids(_f.fx))
ff.update(
    net_code=_f.net_code, net_name=_f.net_name, region=_f.region, city=_f.city,
    cust_no=_f.cust_no, cust_name=_f.cust_name, icp=_f.icp,
    addr=_f.addr, suburb=_f.suburb,
    plan_code="DEMO_STD_FXTOU_%s" % _f.slug.upper(),
    ly_from=_f.ly[0], ly_to=_f.ly[1], ly_temp=_f.ly_temp,
    ap_from=_f.ap[0], ap_to=_f.ap[1], ap_temp=_f.ap_temp,
    cur_from=_f.cur[0], cur_to=_f.cur[1], cur_temp=_f.cur_temp,
    as_at=_f.as_at,
    as_at_next=(_as_at + datetime.timedelta(days=1)).isoformat(),
    ap_dst=_dst_end.isoformat(),
    ly_issue=_ly_issue, ly_due=_ly_due, ap_issue=_ap_issue, ap_due=_ap_due,
    cur_days=days_between(*_f.cur),
    n_snap=days_between(_f.cur[0], _f.as_at),
    daily_kwh="%.2f" % _f.daily_kwh,
    band_pct=_f.band_pct, band_pct_txt=round(_f.band_pct * 100),
    anom_pct=_f.anom_pct, model_version=_f.model_version,
    weight=WEIGHT, heat=HEAT, heat_sens=HEAT_SENS,
    loss=LOSS_FACTOR, loss_pct=round(LOSS_FACTOR * 100, 2),
    **{("r_" + k): v for k, v in RATES.items()})

# Four temperature expressions, one per date column the SQL has to hand:
# the reads CTE, the daily-usage spine, the remaining-days lateral, and the
# self-check's whole-period rebuild. All four are the same formula, which is
# what keeps mart, meter and forecast from disagreeing about the weather.
ff["temp_d"] = temp_expr("d", ff)
ff["temp_sp"] = temp_expr("sp.d", ff)
ff["temp_g"] = temp_expr("g.d", ff)
ff["temp_v"] = temp_expr("v.d", ff)

ff["catalogue_src"] = CATALOGUE_SRC.format(**ff)
ff["replica"] = REPLICA_SQL.format(**ff)

_fpath = outfile(_f.fx, "bill_%s" % _f.slug)
with open(_fpath, "w") as fh:
    fh.write(FORECAST_TEMPLATE.format(**ff))
print("wrote", _fpath)
