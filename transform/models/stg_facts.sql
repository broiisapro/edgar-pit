select distinct * from {{ source('raw', 'facts_raw') }}
