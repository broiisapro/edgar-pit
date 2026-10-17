{% set inject_future_ratio = var('inject_future_ratio', false) %}

with fundamentals as (

    select
        cik,
        field,
        period_start,
        period_end,
        period_type,
        accession,
        acceptance_ts,
        value,
        currency,
        version
    from {{ ref('fundamentals_pit') }}
    where period_type in ('FY', 'Q')

),

ratio_definitions as (

    select * from (
        values
            ('roe', 'net_income', 'total_equity'),
            ('leverage', 'total_liabilities', 'total_equity'),
            ('net_margin', 'net_income', 'revenue')
    ) as definitions (ratio, numerator_field, denominator_field)

),

pairs as (

    select
        definitions.ratio,
        definitions.numerator_field,
        definitions.denominator_field,
        numerator.cik,
        numerator.period_start as numerator_period_start,
        denominator.period_start as denominator_period_start,
        numerator.period_end,
        numerator.period_type,
        numerator.accession as numerator_accession,
        numerator.acceptance_ts as numerator_acceptance_ts,
        numerator.value as numerator_value,
        numerator.currency as numerator_currency,
        denominator.accession as denominator_accession,
        denominator.acceptance_ts as denominator_acceptance_ts,
        denominator.value as denominator_value,
        denominator.currency as denominator_currency
    from ratio_definitions as definitions
    inner join fundamentals as numerator
        on numerator.field = definitions.numerator_field
    inner join fundamentals as denominator
        on denominator.cik = numerator.cik
        and denominator.field = definitions.denominator_field
        and denominator.period_end = numerator.period_end
        and denominator.period_type = numerator.period_type
        and (
            (numerator.period_start is null and denominator.period_start is null)
            or (numerator.period_start is not null and denominator.period_start is null)
            or (
                numerator.period_start is not null
                and denominator.period_start = numerator.period_start
            )
        )

),

candidates as (

    select
        ratio,
        numerator_field,
        denominator_field,
        cik,
        numerator_period_start,
        denominator_period_start,
        period_end,
        period_type,
        numerator_acceptance_ts as candidate_ts
    from pairs

    union

    select
        ratio,
        numerator_field,
        denominator_field,
        cik,
        numerator_period_start,
        denominator_period_start,
        period_end,
        period_type,
        denominator_acceptance_ts as candidate_ts
    from pairs

),

numerator_at as (

    select * exclude (row_num)
    from (
        select
            candidates.ratio,
            candidates.numerator_field,
            candidates.denominator_field,
            candidates.cik,
            candidates.numerator_period_start,
            candidates.denominator_period_start,
            candidates.period_end,
            candidates.period_type,
            candidates.candidate_ts,
            numerator.accession as numerator_accession,
            numerator.acceptance_ts as numerator_acceptance_ts,
            numerator.value as numerator_value,
            numerator.currency as numerator_currency,
            row_number() over (
                partition by
                    candidates.ratio,
                    candidates.cik,
                    candidates.numerator_period_start,
                    candidates.denominator_period_start,
                    candidates.period_end,
                    candidates.period_type,
                    candidates.candidate_ts
                order by numerator.acceptance_ts desc, numerator.version desc
            ) as row_num
        from candidates
        inner join fundamentals as numerator
            on numerator.cik = candidates.cik
            and numerator.field = candidates.numerator_field
            and numerator.period_end = candidates.period_end
            and numerator.period_type = candidates.period_type
            and numerator.period_start is not distinct from candidates.numerator_period_start
            and numerator.acceptance_ts <= candidates.candidate_ts
    )
    where row_num = 1

),

denominator_at as (

    select * exclude (row_num)
    from (
        select
            candidates.ratio,
            candidates.cik,
            candidates.numerator_period_start,
            candidates.denominator_period_start,
            candidates.period_end,
            candidates.period_type,
            candidates.candidate_ts,
            denominator.field as denominator_field,
            denominator.accession as denominator_accession,
            denominator.acceptance_ts as denominator_acceptance_ts,
            denominator.value as denominator_value,
            denominator.currency as denominator_currency,
            row_number() over (
                partition by
                    candidates.ratio,
                    candidates.cik,
                    candidates.numerator_period_start,
                    candidates.denominator_period_start,
                    candidates.period_end,
                    candidates.period_type,
                    candidates.candidate_ts
                order by denominator.acceptance_ts desc, denominator.version desc
            ) as row_num
        from candidates
        inner join fundamentals as denominator
            on denominator.cik = candidates.cik
            and denominator.field = candidates.denominator_field
            and denominator.period_end = candidates.period_end
            and denominator.period_type = candidates.period_type
            and denominator.period_start is not distinct from candidates.denominator_period_start
            and denominator.acceptance_ts <= candidates.candidate_ts
    )
    where row_num = 1

),

ratios as (

    select
        numerator_at.cik,
        numerator_at.ratio,
        coalesce(
            numerator_at.numerator_period_start, numerator_at.denominator_period_start
        ) as period_start,
        numerator_at.period_end,
        numerator_at.period_type,
        greatest(
            numerator_at.numerator_acceptance_ts, denominator_at.denominator_acceptance_ts
        ) as asof_ts,
        numerator_at.numerator_value / denominator_at.denominator_value as value,
        numerator_at.numerator_currency as currency,
        numerator_at.numerator_field,
        numerator_at.numerator_accession,
        numerator_at.numerator_acceptance_ts,
        denominator_at.denominator_field,
        denominator_at.denominator_accession,
        denominator_at.denominator_acceptance_ts
    from numerator_at
    inner join denominator_at
        on denominator_at.ratio = numerator_at.ratio
        and denominator_at.cik = numerator_at.cik
        and denominator_at.numerator_period_start
            is not distinct from numerator_at.numerator_period_start
        and denominator_at.denominator_period_start
            is not distinct from numerator_at.denominator_period_start
        and denominator_at.period_end = numerator_at.period_end
        and denominator_at.period_type = numerator_at.period_type
        and denominator_at.candidate_ts = numerator_at.candidate_ts
    where
        numerator_at.numerator_currency is not null
        and numerator_at.numerator_currency = denominator_at.denominator_currency
        and denominator_at.denominator_value <> 0

)

select * from ratios

{% if inject_future_ratio %}
union all
select
    * replace (
        asof_ts + interval '10 years' as asof_ts,
        numerator_acceptance_ts + interval '10 years 1 day' as numerator_acceptance_ts
    )
from (
    select * from ratios
    order by cik, ratio, period_end, asof_ts
    limit 1
)
{% endif %}
