-- Only share-based units can carry a split adjustment (decision A-25).
select *
from {{ ref('fundamentals_pit') }}
where restatement_kind = 'split_adjustment'
  and unit not in ('shares') and unit not like '%/shares'
