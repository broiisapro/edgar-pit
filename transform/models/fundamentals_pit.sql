with facts as (

    select * from {{ ref('int_facts_mapped') }}

),

fy_ends as (

    select distinct
        cik,
        period_end as fy_end
    from {{ ref('stg_facts') }}
    where
        period_start is not null
        and date_diff('day', period_start, period_end) between 350 and 380

),

period_typed as (

    select
        facts.*,
        case
            when facts.period_start is null and fy_ends.fy_end is not null then 'FY'
            when facts.period_start is null then 'Q'
            when date_diff('day', facts.period_start, facts.period_end) between 350 and 380 then 'FY'
            when date_diff('day', facts.period_start, facts.period_end) between 80 and 100 then 'Q'
            else 'OTHER'
        end as period_type
    from facts
    left join fy_ends
        on fy_ends.cik = facts.cik and fy_ends.fy_end = facts.period_end

),

concept_selected as (

    select * exclude (concept_row)
    from (
        select
            *,
            row_number() over (
                partition by cik, field, period_start, period_end, accession
                order by concept_rank, unit, value
            ) as concept_row
        from period_typed
    )
    where concept_row = 1

),

versioned as (

    select
        *,
        row_number() over (
            partition by cik, field, period_start, period_end
            order by acceptance_ts, accession
        ) as version
    from concept_selected

),

with_first as (

    select
        *,
        first_value(value) over (
            partition by cik, field, period_start, period_end
            order by acceptance_ts, accession
            rows between unbounded preceding and unbounded following
        ) as first_value
    from versioned

)

select
    with_first.cik,
    universe.ticker,
    with_first.field,
    with_first.period_start,
    with_first.period_end,
    with_first.period_type,
    with_first.accession,
    with_first.form,
    with_first.acceptance_ts,
    with_first.ts_imputed,
    with_first.value,
    with_first.unit,
    case
        when regexp_matches(with_first.unit, '^[A-Z]{3}$') then with_first.unit
        when regexp_matches(with_first.unit, '^[A-Z]{3}/shares$')
            then split_part(with_first.unit, '/', 1)
        else null
    end as currency,
    with_first.source_taxonomy,
    with_first.source_concept,
    with_first.concept_rank,
    with_first.version,
    with_first.first_value,
    abs(with_first.value - with_first.first_value) > 0.005 * abs(with_first.first_value)
        as is_restatement,
    case
        when with_first.first_value = 0 then null
        else (with_first.value - with_first.first_value) / with_first.first_value
    end as pct_change_vs_first
from with_first
left join {{ source('raw', 'universe') }} as universe
    on universe.cik = with_first.cik
