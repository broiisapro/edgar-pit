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

),

ratioed as (

    select
        *,
        abs(value - first_value) > 0.005 * abs(first_value) as is_restatement,
        value / first_value as value_ratio,
        first_value / value as inverse_ratio
    from with_first

),

classified as (

    -- A restatement is a split adjustment when its share-based unit family
    -- moved by a near-integer factor (forward or reverse); every other
    -- restatement is a revision (decision A-25). Division by zero yields
    -- inf/nan, which never matches the factor list, but the `none` branch and
    -- the unit-family branch keep those rows on `revision` anyway.
    select
        *,
        case
            when not is_restatement then 'none'
            when unit_family in ('shares', 'currency_per_share')
                and (
                    (
                        round(value_ratio)
                            in (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)
                        and abs(value_ratio - round(value_ratio)) <= 0.005 * round(value_ratio)
                    )
                    or (
                        round(inverse_ratio)
                            in (2, 3, 4, 5, 6, 7, 8, 10, 15, 20, 25, 30, 40, 50, 100)
                        and abs(inverse_ratio - round(inverse_ratio))
                            <= 0.005 * round(inverse_ratio)
                    )
                )
                then 'split_adjustment'
            else 'revision'
        end as restatement_kind
    from ratioed

)

select
    classified.cik,
    universe.ticker,
    classified.field,
    classified.period_start,
    classified.period_end,
    classified.period_type,
    classified.accession,
    classified.form,
    classified.acceptance_ts,
    classified.ts_imputed,
    classified.value,
    classified.unit,
    case
        when regexp_matches(classified.unit, '^[A-Z]{3}$') then classified.unit
        when regexp_matches(classified.unit, '^[A-Z]{3}/shares$')
            then split_part(classified.unit, '/', 1)
        else null
    end as currency,
    classified.source_taxonomy,
    classified.source_concept,
    classified.concept_rank,
    classified.version,
    classified.first_value,
    classified.is_restatement,
    case
        when classified.first_value = 0 then null
        else (classified.value - classified.first_value) / classified.first_value
    end as pct_change_vs_first,
    classified.restatement_kind
from classified
left join {{ source('raw', 'universe') }} as universe
    on universe.cik = classified.cik
