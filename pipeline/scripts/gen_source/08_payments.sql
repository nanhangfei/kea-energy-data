-- =====================================================================
-- src_billing.payments -- transaction detail, and the hardship signals
-- that only exist in it.
--
-- RUNS AFTER 04_rate_bills.sql, and it has to: a payment is against an
-- AMOUNT, and amounts do not exist until the invoices are rated. Doing
-- this in Python would mean the generator deciding what a bill costs,
-- which is the thing gen_billing.py's own docstring refuses to do.
--
-- WHY THIS TABLE EXISTS AT SPRINT 3 rather than Sprint 4. The PRD's
-- hardship feature triggers on LANGUAGE -- the customer says something
-- that sounds like difficulty. The Consumer Care Obligations it cites
-- require the retailer to IDENTIFY customers who may be in payment
-- difficulty. Those are different duties, and the households at highest
-- risk are frequently the ones who say nothing.
--
-- Detection needs no data. Identification needs this.
--
-- THE PATTERNS ARE PLANTED ON PURPOSE, the same way Stage 2 plants three
-- orphan ICPs and two unknown plan codes. A triage rule with nothing to
-- find cannot be demonstrated, and a cohort where everyone pays on time
-- is not evidence that the rule works -- it is evidence that nothing was
-- tested. Every profile below is reproducible from a hash of the account
-- number: never random, never the clock.
--
--   PAY_ON_TIME    ~85%  full amount, on or before the due date
--   LATE_STREAK      6%  full amount, 8-30 days late, every bill
--   DD_FAILURE       4%  direct debit bounces, then a manual payment
--   PART_PAYMENT     3%  pays 40-70%, arrears accumulate
--   STOPPED          2%  paid, then stopped -- the severe case
--
-- ⚠️ A FAILED PAYMENT IS A ROW. Modelling failure as the absence of a
-- payment makes it indistinguishable from a bill that is merely not due
-- yet, and a bounced direct debit is the single strongest early signal
-- here. FAILED_FLAG = 'Y' with a reason is what makes it findable.
--
-- ⚠️ THE MOST RECENT INVOICES ARE UNPAID, and that is not a defect. A
-- bill issued days before the span ends has not reached its due date.
-- Anything counting "unpaid invoices" as a hardship signal has to
-- exclude them, and this is where that becomes visible.
--
-- Scoped to generated accounts (ICP_NUMBER LIKE '09%'). The nine fixtures
-- carry hand-written invoices whose totals are reconciled to the cent;
-- attaching payments to them would rewrite balances the fixtures assert.
-- =====================================================================

BEGIN;

DELETE FROM src_billing.payments p
 USING src_billing.accounts a
 WHERE p.ACCT_ID = a.ACCT_ID AND a.ICP_NUMBER LIKE '09%';

-- One profile per account, stable across runs.
CREATE TEMP TABLE _profile ON COMMIT DROP AS
SELECT a.ACCT_ID, a.PAY_METHOD, a.ACCT_NO,
       -- 28 bits, not 32: bit(32)::int is signed and comes back negative
       -- half the time, which would send half the cohort to the ELSE branch.
       ('x' || substr(md5(a.ACCT_NO || 'payprofile'), 1, 7))::bit(28)::int % 100 AS r
FROM src_billing.accounts a
WHERE a.ICP_NUMBER LIKE '09%';

ALTER TABLE _profile ADD COLUMN profile text;
UPDATE _profile SET profile =
    CASE WHEN r < 85 THEN 'PAY_ON_TIME'
         WHEN r < 91 THEN 'LATE_STREAK'
         WHEN r < 95 THEN 'DD_FAILURE'
         WHEN r < 98 THEN 'PART_PAYMENT'
         ELSE             'STOPPED' END;

-- A compact demo cohort is intentionally small enough that a 2% hash bucket
-- can contain nobody. Keep the natural distribution when it exists; only
-- fill a missing risk profile by deterministically reassigning one otherwise
-- on-time payer. This keeps every Sprint 3 identification path demonstrable
-- without inflating the cohort merely to win a probability bet.
UPDATE _profile SET profile = 'LATE_STREAK'
WHERE ACCT_ID = (SELECT ACCT_ID FROM _profile
                 WHERE profile = 'PAY_ON_TIME'
                   AND NOT EXISTS (SELECT 1 FROM _profile WHERE profile = 'LATE_STREAK')
                 ORDER BY md5(ACCT_NO || 'ensure-late') LIMIT 1);
UPDATE _profile SET profile = 'DD_FAILURE'
WHERE ACCT_ID = (SELECT ACCT_ID FROM _profile
                 WHERE profile = 'PAY_ON_TIME'
                   AND NOT EXISTS (SELECT 1 FROM _profile WHERE profile = 'DD_FAILURE')
                 ORDER BY md5(ACCT_NO || 'ensure-dd') LIMIT 1);
UPDATE _profile SET profile = 'PART_PAYMENT'
WHERE ACCT_ID = (SELECT ACCT_ID FROM _profile
                 WHERE profile = 'PAY_ON_TIME'
                   AND NOT EXISTS (SELECT 1 FROM _profile WHERE profile = 'PART_PAYMENT')
                 ORDER BY md5(ACCT_NO || 'ensure-part') LIMIT 1);
UPDATE _profile SET profile = 'STOPPED'
WHERE ACCT_ID = (SELECT ACCT_ID FROM _profile
                 WHERE profile = 'PAY_ON_TIME'
                   AND NOT EXISTS (SELECT 1 FROM _profile WHERE profile = 'STOPPED')
                 ORDER BY md5(ACCT_NO || 'ensure-stopped') LIMIT 1);

-- Invoices eligible for a payment: due at least five days before the data
-- ends, so the newest bills stay legitimately outstanding.
CREATE TEMP TABLE _due ON COMMIT DROP AS
WITH horizon AS (
    SELECT max(PERIOD_TO) - interval '5 days' AS cutoff FROM src_billing.invoices
)
SELECT i.INV_ID, i.ACCT_ID, i.TOTAL_AMT, i.DUE_DT,
       row_number() OVER (PARTITION BY i.ACCT_ID ORDER BY i.PERIOD_TO) AS seq,
       count(*)     OVER (PARTITION BY i.ACCT_ID)                      AS n_inv
FROM src_billing.invoices i
JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
CROSS JOIN horizon h
WHERE a.ICP_NUMBER LIKE '09%'
  AND i.TOTAL_AMT > 0
  AND i.DUE_DT <= h.cutoff;

-- ---------------------------------------------------------------------
-- 1. The payments that succeed.
--
-- STOPPED pays only its first 60% of bills; the arrears after that are
-- the signal. PART_PAYMENT pays a stable fraction, so the shortfall
-- compounds bill after bill -- an arrears TRAJECTORY rather than one
-- missed payment, which is what distinguishes difficulty from an
-- oversight.
-- ---------------------------------------------------------------------
INSERT INTO src_billing.payments
    (ACCT_ID, INV_ID, PAID_TS, PAY_AMT, PAY_METHOD, FAILED_FLAG)
SELECT d.ACCT_ID, d.INV_ID,
       d.DUE_DT + CASE p.profile
           WHEN 'LATE_STREAK' THEN ((d.seq * 7 + 8) % 23 + 8) * interval '1 day'
           WHEN 'DD_FAILURE'  THEN interval '4 days'   -- after the bounce
           ELSE (-((d.seq * 3) % 4)) * interval '1 day'
       END,
       ROUND(d.TOTAL_AMT * CASE p.profile
           WHEN 'PART_PAYMENT' THEN 0.40 + ((d.seq * 11) % 31) / 100.0
           ELSE 1.00 END, 2),
       p.PAY_METHOD, 'N'
FROM _due d JOIN _profile p ON p.ACCT_ID = d.ACCT_ID
WHERE p.profile <> 'STOPPED'
   OR d.seq <= GREATEST(1, (d.n_inv * 6) / 10);

-- ---------------------------------------------------------------------
-- 2. The direct debits that bounce.
--
-- Written BEFORE the successful retry above, four days earlier, so the
-- pair reads the way it does on a real account: an attempt, a failure,
-- a manual payment. The reason string is what a triage rule quotes back
-- to a human, so it is a real bank reason and not a code.
-- ---------------------------------------------------------------------
INSERT INTO src_billing.payments
    (ACCT_ID, INV_ID, PAID_TS, PAY_AMT, PAY_METHOD, FAILED_FLAG, FAIL_REASON)
SELECT d.ACCT_ID, d.INV_ID, d.DUE_DT, d.TOTAL_AMT, 'DD', 'Y',
       CASE (d.seq % 3) WHEN 0 THEN 'Insufficient funds'
                        WHEN 1 THEN 'Payment stopped by customer'
                        ELSE        'Account closed' END
FROM _due d JOIN _profile p ON p.ACCT_ID = d.ACCT_ID
WHERE p.profile = 'DD_FAILURE'
  AND d.seq % 2 = 0;          -- every other bill, not every one

-- ---------------------------------------------------------------------
-- 3. PAYMENTS_APPL on the invoice.
--
-- Successful payments only. A bounced direct debit applied nothing, and
-- an invoice whose PAYMENTS_APPL included it would show a customer as
-- having paid when the money never moved.
-- ---------------------------------------------------------------------
UPDATE src_billing.invoices i
   SET PAYMENTS_APPL = s.paid
  FROM (SELECT p.INV_ID, SUM(p.PAY_AMT) AS paid
          FROM src_billing.payments p
         WHERE p.FAILED_FLAG = 'N' AND p.INV_ID IS NOT NULL
         GROUP BY p.INV_ID) s
 WHERE s.INV_ID = i.INV_ID;

DO $$
DECLARE n_pay int; n_fail int; n_short int; n_prof text;
BEGIN
    SELECT count(*) INTO n_pay FROM src_billing.payments;
    SELECT count(*) INTO n_fail FROM src_billing.payments WHERE FAILED_FLAG = 'Y';
    SELECT count(*) INTO n_short
      FROM src_billing.invoices i JOIN src_billing.accounts a ON a.ACCT_ID = i.ACCT_ID
     WHERE a.ICP_NUMBER LIKE '09%' AND i.TOTAL_AMT > 0
       AND i.PAYMENTS_APPL > 0 AND i.PAYMENTS_APPL < i.TOTAL_AMT;

    IF n_fail = 0 THEN
        RAISE EXCEPTION 'no failed direct debits: hardship triage has nothing to find';
    END IF;
    IF n_short = 0 THEN
        RAISE EXCEPTION 'no part payments: an arrears trajectory cannot be built';
    END IF;

    SELECT string_agg(profile || '=' || c, '  ' ORDER BY profile) INTO n_prof
      FROM (SELECT profile, count(*) c FROM _profile GROUP BY profile) t;
    RAISE NOTICE 'payments: % rows, % failed, % part-paid invoices', n_pay, n_fail, n_short;
    RAISE NOTICE 'profiles: %', n_prof;
END $$;

COMMIT;
