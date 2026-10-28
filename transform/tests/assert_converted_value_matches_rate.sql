-- A converted CAD row divides its value by the observation it records; a USD
-- row keeps its value and at 1.0 (decision A-32). A mismatch means value_usd
-- was computed from a different rate than fx_rate reports.
select *
from {{ ref('fundamentals_pit_usd') }}
where converted
  and currency = 'CAD'
  and (
      fx_rate is null
      or fx_rate <= 0
      or abs(value_usd - value / fx_rate) > 1e-9 * greatest(abs(value_usd), 1.0)
  )
