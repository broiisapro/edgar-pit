select
    filings.*,
    universe.ticker
from {{ ref('stg_filings') }} as filings
left join {{ source('raw', 'universe') }} as universe
    on universe.cik = filings.cik
