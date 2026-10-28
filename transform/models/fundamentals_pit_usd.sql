{% set inject_future_fx = var('inject_future_fx', false) %}

-- Decision A-32: convert CAD fundamentals to USD with the latest Bank of
-- Canada FXUSDCAD observation on or before the period end, and move `asof_ts`
-- to the later of the filing's acceptance time and the rate's known time, so a
-- converted value is never visible before its rate was published. USD rows use
-- no observation (rate 1.0); every other currency is left unconverted and
-- flagged (decision A-38).

with rates as (

    select rate_date, value, known_at
    from {{ source('raw', 'fx_daily') }}
    where series = 'FXUSDCAD'

    {% if inject_future_fx %}
    -- Test-only control: an observation dated after every period end. It must
    -- change no row, because the join only takes rates on or before the period
    -- end (see tests/test_fundamentals_usd.py).
    union all
    select date '2099-12-31' as rate_date, 999.0 as value,
        timestamptz '2099-12-31 22:00:00+00' as known_at
    {% endif %}

),

matched as (

    select
        fundamentals.*,
        rate.rate_date as rate_date,
        rate.value as rate_value,
        rate.known_at as rate_known_at
    from {{ ref('fundamentals_pit') }} as fundamentals
    left join lateral (
        select rates.rate_date, rates.value, rates.known_at
        from rates
        where rates.rate_date <= fundamentals.period_end
        order by rates.rate_date desc
        limit 1
    ) as rate
        on true

)

select
    matched.* exclude (rate_date, rate_value, rate_known_at),
    case
        when matched.currency = 'USD' then matched.value
        when matched.currency = 'CAD' and matched.rate_value is not null
            then matched.value / matched.rate_value
    end as value_usd,
    case
        when matched.currency = 'USD' then 1.0
        when matched.currency = 'CAD' then matched.rate_value
    end as fx_rate,
    case
        when matched.currency = 'USD' then null
        when matched.currency = 'CAD' then matched.rate_date
    end as fx_rate_date,
    case
        when matched.currency = 'USD' then null
        when matched.currency = 'CAD' then matched.rate_known_at
    end as fx_known_at,
    case
        when matched.currency = 'USD' then true
        when matched.currency = 'CAD' and matched.rate_value is not null then true
        else false
    end as converted,
    case
        when matched.currency = 'USD' then matched.acceptance_ts
        when matched.currency = 'CAD' and matched.rate_value is not null
            then greatest(matched.acceptance_ts, matched.rate_known_at)
        else matched.acceptance_ts
    end as asof_ts,
    case
        when matched.currency = 'USD' then null
        when matched.currency = 'CAD' and matched.rate_value is null then 'no_rate'
        when matched.currency = 'CAD' then null
        else 'currency_out_of_scope'
    end as unconverted_reason
from matched
