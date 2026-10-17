select *
from {{ ref('fundamentals_pit') }}
where version = 1 and is_restatement
