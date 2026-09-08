-- billing.payment  <-  fct_payment
--
-- The billing source spells payment methods DD / BT / CARD / OTHER while the
-- serving column is common.payment_method. Map at the export boundary so the
-- source spelling remains visible upstream and Postgres receives enum values.
-- source_bill_id is already null when its source invoice could not land in
-- billing.bill, preserving the optional FK rather than creating a dangling one.

select
    payment_id,
    source_account_id as account_id,
    source_bill_id    as bill_id,
    paid_at,
    amount,
    case payment_method
        when 'DD'   then 'direct_debit'
        when 'BT'   then 'bank_transfer'
        when 'CARD' then 'card'
        else 'other'
    end              as method,
    is_failed,
    failure_reason
from KEA.KEA_GOLD.FCT_PAYMENT
order by payment_id
