-- Conversion may only use an observation dated on or before the period end
-- (decision A-32): a rate published later than the period cannot price it, and
-- a future-dated observation must never be selected. The `inject_future_fx`
-- build adds an observation dated 2099-12-31 to prove that.
select *
from {{ ref('fundamentals_pit_usd') }}
where fx_rate_date > period_end
