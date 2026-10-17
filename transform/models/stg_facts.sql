select * from {{ source('raw', 'facts_raw') }}
