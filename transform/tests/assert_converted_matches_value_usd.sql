-- A row is converted exactly when it has a USD value and no unconverted
-- reason (decisions A-32, A-38); every other row keeps value_usd and the
-- reason in step.
select *
from {{ ref('fundamentals_pit_usd') }}
where converted <> (value_usd is not null)
   or (unconverted_reason is null) <> (value_usd is not null)
   or (currency = 'USD' and converted and fx_rate <> 1.0)
   or (converted and unconverted_reason is not null)
