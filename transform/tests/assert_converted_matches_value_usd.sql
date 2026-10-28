-- A row is converted exactly when it has a USD value and no unconverted
-- reason (decisions A-32, A-38): every other row keeps value_usd and the
-- reason in step. A converted row always records the rate it used, and a USD
-- row always converts at 1.0.
select *
from {{ ref('fundamentals_pit_usd') }}
where converted <> (value_usd is not null)
   or converted <> (unconverted_reason is null)
   or (converted and fx_rate is null)
   or (currency = 'USD' and converted and fx_rate is distinct from 1.0)
