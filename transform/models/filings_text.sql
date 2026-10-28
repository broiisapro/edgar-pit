select
    filings_text.*,
    universe.ticker
from {{ source('raw', 'filings_text') }} as filings_text
left join {{ source('raw', 'universe') }} as universe
    on universe.cik = filings_text.cik
