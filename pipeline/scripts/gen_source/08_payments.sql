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
--   SLIDING        3 accts  on time, then drifts later and short-pays
--                           after a bill well above its usual (Sprint 4)
--   SLIDING_SUDDEN 1 acct   on time, then stops with no warning (Sprint 4)
--
-- The two Sprint 4 profiles are counts, not shares, and are taken only
-- from PAY_ON_TIME after the ensure-steps, so every Sprint 3 profile keeps
-- its accounts and every v1 flag stays where it was. Their answers --
-- trigger bill, drift, arrears onset -- go to gen_audit.planted_hardship.
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
-- 0. Sprint 4: accounts that slide into arrears.
--
-- A SLIDING account pays on time until one bill arrives at >= 130% of the
-- median of its previous six (its usual payment: an on-time payer pays the
-- whole bill). Around that bill its payments drift later, then it pays
-- only about its usual amount, so a shortfall opens on the big bill and
-- grows on every bill after it. That is the story the early warning has to
-- read weeks before the arrears are real.
--
-- SLIDING_SUDDEN pays on time and then stops at an ordinary bill, with no
-- drift and no big bill first. The early warning is expected to miss it;
-- it is there so the evaluation can say so rather than imply otherwise.
--
-- EXCLUDED: the accounts a source defect already breaks. 07 runs AFTER
-- this file, so its orphans (first three and last two generated accounts
-- by ACCT_NO) are recomputed here; 05 §8 and §9 break the first two
-- generated ICPs. planted.defect_targets() applies the same rule to the
-- planted loads.
-- ---------------------------------------------------------------------
CREATE TEMP TABLE _excluded ON COMMIT DROP AS
(SELECT ACCT_ID FROM src_billing.accounts
  WHERE ICP_NUMBER LIKE '09%' ORDER BY ACCT_NO LIMIT 3)
UNION
(SELECT ACCT_ID FROM src_billing.accounts
  WHERE ICP_NUMBER LIKE '09%' ORDER BY ACCT_NO DESC LIMIT 2)
UNION
(SELECT ACCT_ID FROM src_billing.accounts
  WHERE ICP_NUMBER IN (SELECT ICP_NUMBER FROM src_billing.accounts
                        WHERE ICP_NUMBER LIKE '09%'
                        ORDER BY ICP_NUMBER LIMIT 2));

-- Each due bill against the median of the six before it.
CREATE TEMP TABLE _usual ON COMMIT DROP AS
SELECT d.ACCT_ID, d.INV_ID, d.seq, d.n_inv, d.TOTAL_AMT,
       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY e.TOTAL_AMT)
          FROM _due e
         WHERE e.ACCT_ID = d.ACCT_ID
           AND e.seq BETWEEN d.seq - 6 AND d.seq - 1)::numeric(12,2) AS usual
FROM _due d
WHERE d.seq >= 7;

-- The trigger: the first bill after the sixth at >= 130% of usual, with
-- at least three due bills after it so the slide has room to show.
CREATE TEMP TABLE _slide ON COMMIT DROP AS
SELECT DISTINCT ON (u.ACCT_ID)
       u.ACCT_ID, u.seq AS trigger_seq, u.usual,
       -- 1 or 2 drifting bills before the trigger, each paid `step` days
       -- later than the one before; the trigger itself is paid at most 7
       -- days late, so the drift is visible before any v1 late streak.
       1 + ('x' || substr(md5(p.ACCT_NO || 'slide-k'), 1, 7))::bit(28)::int % 2 AS k,
       'SLIDING'::text AS profile
FROM _usual u
JOIN _profile p ON p.ACCT_ID = u.ACCT_ID
WHERE p.profile = 'PAY_ON_TIME'
  AND u.ACCT_ID NOT IN (SELECT ACCT_ID FROM _excluded)
  AND u.TOTAL_AMT >= 1.3 * u.usual
  AND u.seq <= u.n_inv - 3
ORDER BY u.ACCT_ID, u.seq;

DELETE FROM _slide WHERE ACCT_ID NOT IN (
    SELECT s.ACCT_ID FROM _slide s JOIN _profile p ON p.ACCT_ID = s.ACCT_ID
    ORDER BY md5(p.ACCT_NO || 'sliding') LIMIT 3);

-- The sudden stop: the last ordinary bill (below 130% of usual) that
-- still leaves three due bills after it, on an account not already sliding.
INSERT INTO _slide (ACCT_ID, trigger_seq, usual, k, profile)
SELECT ACCT_ID, trigger_seq, usual, 0, 'SLIDING_SUDDEN'
FROM (
    SELECT DISTINCT ON (u.ACCT_ID) u.ACCT_ID, u.seq AS trigger_seq, u.usual, p.ACCT_NO
    FROM _usual u
    JOIN _profile p ON p.ACCT_ID = u.ACCT_ID
    WHERE p.profile = 'PAY_ON_TIME'
      AND u.ACCT_ID NOT IN (SELECT ACCT_ID FROM _excluded)
      AND u.ACCT_ID NOT IN (SELECT ACCT_ID FROM _slide)
      AND u.TOTAL_AMT < 1.3 * u.usual
      AND u.seq <= u.n_inv - 3
    ORDER BY u.ACCT_ID, u.seq DESC
) c
ORDER BY md5(ACCT_NO || 'sliding-sudden')
LIMIT 1;

UPDATE _profile p SET profile = s.profile FROM _slide s WHERE s.ACCT_ID = p.ACCT_ID;

-- Days after (or, negative, before) the due date, and the fraction paid.
--   SLIDING, drift step 3 days (k = 2) or 5 days (k = 1):
--     seq <  T-k-1   as PAY_ON_TIME
--     seq =  T-k-1   3 days early          (the baseline the drift starts from)
--     T-k .. T       step, 2*step, ... minus 3 days: k = 2 gives 0, 3, 6;
--                    k = 1 gives 2, 7
--     seq >  T       7 days late
--     seq >= T       pays min(usual, 92% of the bill)
--   SLIDING_SUDDEN: as PAY_ON_TIME before T, nothing from T on.
CREATE TEMP TABLE _slide_plan ON COMMIT DROP AS
SELECT d.INV_ID, d.ACCT_ID, s.profile, d.seq, s.trigger_seq,
       CASE WHEN s.profile = 'SLIDING_SUDDEN' AND d.seq >= s.trigger_seq THEN NULL
            WHEN s.profile = 'SLIDING' AND d.seq > s.trigger_seq THEN 7
            WHEN s.profile = 'SLIDING' AND d.seq >= s.trigger_seq - s.k THEN
                 (CASE s.k WHEN 2 THEN 3 ELSE 5 END)
                 * (d.seq - (s.trigger_seq - s.k) + 1) - 3
            WHEN s.profile = 'SLIDING' AND d.seq = s.trigger_seq - s.k - 1 THEN -3
            ELSE -((d.seq * 3) % 4)
       END AS days_after_due,
       CASE WHEN s.profile = 'SLIDING' AND d.seq >= s.trigger_seq
            THEN ROUND(LEAST(s.usual, 0.92 * d.TOTAL_AMT), 2)
            ELSE d.TOTAL_AMT
       END AS amount
FROM _due d JOIN _slide s ON s.ACCT_ID = d.ACCT_ID;

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
WHERE p.profile NOT IN ('STOPPED', 'SLIDING', 'SLIDING_SUDDEN')
   OR (p.profile = 'STOPPED' AND d.seq <= GREATEST(1, (d.n_inv * 6) / 10));

-- The sliding accounts, from their plan above.
INSERT INTO src_billing.payments
    (ACCT_ID, INV_ID, PAID_TS, PAY_AMT, PAY_METHOD, FAILED_FLAG)
SELECT d.ACCT_ID, d.INV_ID, d.DUE_DT + s.days_after_due * interval '1 day',
       s.amount, p.PAY_METHOD, 'N'
FROM _due d
JOIN _slide_plan s ON s.INV_ID = d.INV_ID
JOIN _profile p ON p.ACCT_ID = d.ACCT_ID
WHERE s.days_after_due IS NOT NULL;

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

-- ---------------------------------------------------------------------
-- 4. The planted answers.
--
-- Arrears onset: the first date on which a bill still has >= 5% unpaid 30
-- days after its due date, counting only successful payments made by then,
-- and only where that date is inside the data. The Sprint 4 evaluation
-- uses the same definition.
-- ---------------------------------------------------------------------
DELETE FROM gen_audit.planted_hardship;

INSERT INTO gen_audit.planted_hardship
    (run_id, acct_no, profile, trigger_inv_no, trigger_issued_on,
     usual_amount, drift_from, arrears_onset_on)
SELECT (SELECT max(run_id) FROM gen_audit.runs),
       a.ACCT_NO, s.profile, t.INV_NO, t.ISSUE_DT::date, s.usual,
       (SELECT min((d.DUE_DT + sp.days_after_due * interval '1 day')::date)
          FROM _slide_plan sp JOIN _due d ON d.INV_ID = sp.INV_ID
         WHERE sp.ACCT_ID = s.ACCT_ID AND sp.profile = 'SLIDING'
           AND sp.seq >= s.trigger_seq - s.k),
       (SELECT min((i.DUE_DT + interval '30 days')::date)
          FROM src_billing.invoices i
         WHERE i.ACCT_ID = s.ACCT_ID AND i.TOTAL_AMT > 0
           AND i.DUE_DT + interval '30 days'
               <= (SELECT max(PERIOD_TO) FROM src_billing.invoices)
           AND COALESCE((SELECT sum(p.PAY_AMT) FROM src_billing.payments p
                          WHERE p.INV_ID = i.INV_ID AND p.FAILED_FLAG = 'N'
                            AND p.PAID_TS <= i.DUE_DT + interval '30 days'), 0)
               < 0.95 * i.TOTAL_AMT)
FROM _slide s
JOIN src_billing.accounts a ON a.ACCT_ID = s.ACCT_ID
JOIN _due d ON d.ACCT_ID = s.ACCT_ID AND d.seq = s.trigger_seq
JOIN src_billing.invoices t ON t.INV_ID = d.INV_ID;

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

    IF (SELECT count(*) FROM gen_audit.planted_hardship WHERE profile = 'SLIDING') <> 3
       OR (SELECT count(*) FROM gen_audit.planted_hardship WHERE profile = 'SLIDING_SUDDEN') <> 1 THEN
        RAISE EXCEPTION 'expected 3 SLIDING and 1 SLIDING_SUDDEN accounts, got %',
            (SELECT string_agg(profile, ',') FROM gen_audit.planted_hardship);
    END IF;
    IF EXISTS (SELECT 1 FROM gen_audit.planted_hardship WHERE arrears_onset_on IS NULL) THEN
        RAISE EXCEPTION 'a sliding account never reaches arrears inside the data';
    END IF;

    SELECT string_agg(profile || '=' || c, '  ' ORDER BY profile) INTO n_prof
      FROM (SELECT profile, count(*) c FROM _profile GROUP BY profile) t;
    RAISE NOTICE 'payments: % rows, % failed, % part-paid invoices', n_pay, n_fail, n_short;
    RAISE NOTICE 'profiles: %', n_prof;
END $$;

COMMIT;
