-- Bill-sized evidence for Sprint 3 plan recommendations.
-- Safe on an existing demo volume and deliberately append-only in normal runs.
BEGIN;

CREATE TABLE IF NOT EXISTS kea.plan_bill_example (
    example_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id             bigint        NOT NULL REFERENCES billing.account(account_id) DEFERRABLE,
    bill_id                bigint        NOT NULL REFERENCES billing.bill(bill_id) DEFERRABLE,
    current_plan_id        integer       NOT NULL REFERENCES catalogue.plan(plan_id) DEFERRABLE,
    candidate_plan_id      integer       NOT NULL REFERENCES catalogue.plan(plan_id) DEFERRABLE,
    term_months            smallint      NOT NULL DEFAULT 0,
    period_start           date          NOT NULL,
    period_end             date          NOT NULL,
    period_days            smallint      NOT NULL,
    import_kwh             numeric(12,4) NOT NULL,
    export_kwh             numeric(12,4) NOT NULL DEFAULT 0,
    current_discount_kwh   numeric(12,4) NOT NULL DEFAULT 0,
    candidate_discount_kwh numeric(12,4) NOT NULL DEFAULT 0,
    actual_bill_amount     numeric(12,2) NOT NULL,
    current_modelled_amount numeric(12,2) NOT NULL,
    candidate_modelled_amount numeric(12,2) NOT NULL,
    usage_saving           numeric(12,2) NOT NULL,
    monthly_credit         numeric(12,2) NOT NULL DEFAULT 0,
    current_energy_amount  numeric(12,2) NOT NULL,
    candidate_energy_amount numeric(12,2) NOT NULL,
    current_network_amount numeric(12,2) NOT NULL,
    candidate_network_amount numeric(12,2) NOT NULL,
    current_other_amount   numeric(12,2) NOT NULL,
    candidate_other_amount numeric(12,2) NOT NULL,
    computed_at            timestamptz   NOT NULL,
    UNIQUE (account_id, bill_id, computed_at),
    CONSTRAINT bill_example_term_valid CHECK (term_months IN (0, 6, 12)),
    CONSTRAINT bill_example_dates_ordered CHECK (period_end >= period_start),
    CONSTRAINT bill_example_days_match
        CHECK (period_days = period_end - period_start + 1),
    CONSTRAINT bill_example_usage_nonnegative
        CHECK (import_kwh >= 0 AND export_kwh >= 0
               AND current_discount_kwh >= 0 AND candidate_discount_kwh >= 0),
    CONSTRAINT bill_example_saving_adds_up
        CHECK (ROUND(usage_saving, 2) =
               ROUND(current_modelled_amount - candidate_modelled_amount, 2))
);

CREATE INDEX IF NOT EXISTS idx_plan_bill_example_account
    ON kea.plan_bill_example(account_id, computed_at DESC, period_end DESC);

-- CREATE TABLE IF NOT EXISTS does not change constraints on a volume where an
-- earlier revision of this migration already created the table.  Serving
-- reloads delete and reinsert referenced rows in one transaction, so these
-- checks must be deferrable just like the source-facing bill FKs.
ALTER TABLE kea.plan_bill_example
    ALTER CONSTRAINT plan_bill_example_account_id_fkey DEFERRABLE,
    ALTER CONSTRAINT plan_bill_example_bill_id_fkey DEFERRABLE,
    ALTER CONSTRAINT plan_bill_example_current_plan_id_fkey DEFERRABLE,
    ALTER CONSTRAINT plan_bill_example_candidate_plan_id_fkey DEFERRABLE;

GRANT SELECT ON kea.plan_bill_example TO kea_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON kea.plan_bill_example TO kea_loader;
GRANT USAGE, SELECT ON SEQUENCE kea.plan_bill_example_example_id_seq TO kea_loader;

COMMIT;
