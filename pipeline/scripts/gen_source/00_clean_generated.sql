-- =====================================================================
-- Remove everything the generator made, in foreign-key order.
--
-- Needed because the nine fixtures and the generated cohort SHARE
-- src_metering.networks. 03_reference.sql upserts networks on their
-- business key, so generated connection points attach to the fixtures'
-- own network rows (VECT is network_id 111001, seeded by fixture 01)
-- rather than to duplicates. That is the right behaviour -- one network
-- code, one row -- but it has a consequence:
--
--   sample/01-*.sql begins with DELETE FROM src_metering.networks
--   WHERE network_id = 111001, which FAILS once generated connection
--   points reference it.
--
-- So the fixtures cannot be re-seeded while generated data is present.
-- `make reseed-fixtures` runs this first, in the same transaction.
-- Ordering is fixtures -> generated, never the reverse.
--
-- interval_reads are NOT deleted here: they carry no foreign key and are
-- scoped by source_file, so reloading 18.8 M rows to re-seed nine
-- fixtures would be pointless. load_source.sh deletes them separately.
-- =====================================================================

BEGIN;

DELETE FROM src_billing.payments p
 USING src_billing.accounts a
 WHERE p.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

DELETE FROM src_billing.invoice_lines l
 USING src_billing.invoices i, src_billing.accounts a
 WHERE l.INV_ID = i.INV_ID AND i.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

DELETE FROM src_billing.invoices i
 USING src_billing.accounts a
 WHERE i.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

DELETE FROM src_billing.account_plan_history h
 USING src_billing.accounts a
 WHERE h.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

DELETE FROM src_billing.accounts WHERE ICP_NUMBER LIKE '09%';

DELETE FROM src_billing.customers c
 WHERE NOT EXISTS (SELECT 1 FROM src_billing.accounts a WHERE a.CUST_ID = c.CUST_ID);

DELETE FROM src_metering.registers r
 USING src_metering.meters m, src_metering.connection_points c
 WHERE r.meter_id = m.meter_id AND m.icp_id = c.icp_id
   AND c.icp_identifier LIKE '09%';

DELETE FROM src_metering.meters m
 USING src_metering.connection_points c
 WHERE m.icp_id = c.icp_id AND c.icp_identifier LIKE '09%';

DELETE FROM src_metering.connection_points WHERE icp_identifier LIKE '09%';

DELETE FROM src_catalogue.rate_components WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.tou_windows     WHERE plan_id >= 1000000;
DELETE FROM src_catalogue.price_plans     WHERE plan_id >= 1000000;

COMMIT;
