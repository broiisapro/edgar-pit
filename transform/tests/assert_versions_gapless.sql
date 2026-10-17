with numbered as (

    select
        cik,
        field,
        period_start,
        period_end,
        accession,
        version,
        row_number() over (
            partition by cik, field, period_start, period_end
            order by acceptance_ts, accession
        ) as expected_version
    from {{ ref('fundamentals_pit') }}

)

select *
from numbered
where version <> expected_version
