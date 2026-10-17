with inputs as (

    select
        ratios.cik,
        ratios.ratio,
        ratios.period_start,
        ratios.period_end,
        ratios.asof_ts,
        numerator.currency as numerator_currency,
        denominator.currency as denominator_currency
    from {{ ref('ratios_pit') }} as ratios
    inner join {{ ref('fundamentals_pit') }} as numerator
        on numerator.cik = ratios.cik
        and numerator.field = ratios.numerator_field
        and numerator.accession = ratios.numerator_accession
        and numerator.period_end = ratios.period_end
        and numerator.period_start is not distinct from ratios.period_start
    inner join {{ ref('fundamentals_pit') }} as denominator
        on denominator.cik = ratios.cik
        and denominator.field = ratios.denominator_field
        and denominator.accession = ratios.denominator_accession
        and denominator.period_end = ratios.period_end
        and (
            denominator.period_start is null
            or denominator.period_start = ratios.period_start
        )

)

select *
from inputs
where numerator_currency is distinct from denominator_currency
