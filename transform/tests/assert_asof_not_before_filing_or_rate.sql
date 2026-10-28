-- No row may be known before its filing or before the FX rate it uses; a
-- converted row's asof_ts is exactly the later of the two (decision A-32).
select *
from {{ ref('fundamentals_pit_usd') }}
where asof_ts < acceptance_ts
   or (fx_known_at is not null and asof_ts < fx_known_at)
   or (converted and currency = 'CAD' and asof_ts <> greatest(acceptance_ts, fx_known_at))
