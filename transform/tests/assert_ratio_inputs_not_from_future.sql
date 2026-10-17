select *
from {{ ref('ratios_pit') }}
where
    numerator_acceptance_ts > asof_ts
    or denominator_acceptance_ts > asof_ts
