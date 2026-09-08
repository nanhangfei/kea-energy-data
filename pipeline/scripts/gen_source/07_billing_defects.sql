-- =====================================================================
-- Billing-side defects. Runs AFTER 04_rate_bills.sql, and must.
--
-- These break the cross-system references in src_billing.accounts, and
-- both reasons for the ordering were learned the hard way:
--
--   * 04 UPSERTS accounts from gen_audit.stg_accounts with
--     DO UPDATE SET ICP_NUMBER, PLAN_CODE. Injecting orphans before it
--     runs restores the clean values -- the defects vanish silently.
--
--   * Before 04, generated accounts do not exist at all. The first
--     version of this ran inside 05_defects.sql, updated zero rows, and
--     logged zero defects. Nothing failed; the step was simply inert,
--     and the manifest agreed with itself because it was built from the
--     same empty query.
--
--   * An account whose PLAN_CODE resolves to nothing cannot be rated --
--     no plan version, no lines, no invoice. So the orphan has to be
--     created after its invoices exist, which is what a real orphan is:
--     a reference that USED to resolve.
--
-- src_billing stores ICP_NUMBER and PLAN_CODE as text because the other
-- two systems' ids do not exist in this database, and nothing enforces
-- that they resolve. Design 2.1.1 depends on that and, until now, had
-- nothing to fire on.
-- =====================================================================

BEGIN;

SELECT setseed(0.2291);

DELETE FROM gen_audit.defects WHERE defect_kind LIKE 'src:orphan%';

UPDATE src_billing.accounts SET ICP_NUMBER = ICP_NUMBER || 'X'
WHERE ACCT_ID IN (SELECT ACCT_ID FROM src_billing.accounts
                  WHERE ICP_NUMBER LIKE '09%' AND ICP_NUMBER NOT LIKE '%X'
                  ORDER BY ACCT_NO LIMIT 3);

UPDATE src_billing.accounts SET PLAN_CODE = 'KEA_GONE_VECT_STA'
WHERE ACCT_ID IN (SELECT ACCT_ID FROM src_billing.accounts
                  WHERE ICP_NUMBER LIKE '09%' ORDER BY ACCT_NO DESC LIMIT 2);

INSERT INTO gen_audit.defects (defect_kind, icp_identifier, detail)
SELECT 'src:orphan_icp', ICP_NUMBER, '{}'::jsonb
FROM src_billing.accounts WHERE ICP_NUMBER LIKE '09%X';

INSERT INTO gen_audit.defects (defect_kind, icp_identifier, detail)
SELECT 'src:orphan_plan', ICP_NUMBER, jsonb_build_object('plan_code', PLAN_CODE)
FROM src_billing.accounts WHERE PLAN_CODE = 'KEA_GONE_VECT_STA';

DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM gen_audit.defects WHERE defect_kind LIKE 'src:orphan%';
    IF n <> 5 THEN
        RAISE EXCEPTION 'expected 5 orphan references, logged % -- the step ran against no accounts', n;
    END IF;
    RAISE NOTICE 'billing defects: 3 orphan ICPs, 2 orphan plan codes';
END $$;

COMMIT;
