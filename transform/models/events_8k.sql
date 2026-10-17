select
    events.*,
    universe.ticker
from {{ ref('stg_events_8k') }} as events
left join {{ source('raw', 'universe') }} as universe
    on universe.cik = events.cik
