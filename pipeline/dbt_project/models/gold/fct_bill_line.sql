-- Design §6.4. Grain: bill × line.
--
-- THE PLAN COMES FROM TEMPORAL RESOLUTION, NOT FROM THE INVOICE'S plan_code.
-- The invoice carries a code; the code has up to several versions; the one that
-- applies is whichever brackets the PERIOD END (design §2.1.2). Joining
-- straight to the catalogue on the code would re-rate history against current
-- tariffs and never fail — so this takes plan_id from
-- int_billing__plan_resolution, which resolved it once and quarantined the
-- invoices it could not.
--
-- INVOICES WITH NO COVERING PLAN VERSION ARE EXCLUDED. Their lines exist and
-- are not deleted; they are simply not asserted as rated, because a bill line
-- attributed to a tariff that did not apply is worse than an absent one.
-- int_integrity__rejects carries the reason.
--
-- LOSS QUANTITY IS CARRIED SEPARATELY from quantity. Line losses are ~5.11% of
-- import energy on the printed bill (1.74 kWh on 34.08), and folding them into
-- quantity would make the reconciliation reproduce the total while
-- misrepresenting every line that composes it.

select
    l.invoice_line_id,
    l.invoice_id,
    a.account_id            as source_account_id,
    kc.customer_id,
    ki.icp_id,
    r.plan_id,
    l.charge_group,
    l.flow_direction,
    l.line_description,
    l.tou_band,
    l.quantity,
    l.loss_quantity,
    l.unit_of_measure,
    l.unit_rate,
    l.line_amount,
    l.sort_sequence,
    i.invoice_number,
    i.issued_at,
    i.period_from,
    i.period_to,
    r.period_end_nz
from {{ ref('stg_billing__invoice_lines') }} l
join {{ ref('stg_billing__invoices') }} i
  on i.invoice_id = l.invoice_id
join {{ ref('int_billing__plan_resolution') }} r
  on r.invoice_id = l.invoice_id
 and r.plan_id is not null
join {{ ref('stg_billing__accounts') }} a
  on a.account_id = i.account_id
-- Through customers rather than a correlated subquery: the account carries
-- the source's customer_id, and the key map is on customer_number, so the
-- staging model is the bridge between them.
join {{ ref('stg_billing__customers') }} cu
  on cu.customer_id = a.customer_id
join {{ ref('key_map_customer') }} kc
  on kc.customer_number = cu.customer_number
-- LEFT, because an account whose ICP is absent from metering is design §6.3's
-- orphan -- already quarantined, and its lines should still be visible with a
-- null icp_id rather than vanishing from the fact table entirely.
left join {{ ref('key_map_icp') }} ki
  on ki.icp_number = a.icp_number
