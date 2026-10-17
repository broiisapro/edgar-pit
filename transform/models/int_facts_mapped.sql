with mapped as (

    select
        facts.cik,
        facts.period_start,
        facts.period_end,
        facts.form,
        facts.accession,
        facts.filed,
        facts.unit,
        facts.value,
        facts.taxonomy as source_taxonomy,
        facts.concept as source_concept,
        concept_map.field,
        concept_map.concept_rank,
        concept_map.kind,
        concept_map.unit_family
    from {{ ref('stg_facts') }} as facts
    inner join {{ source('raw', 'concept_map') }} as concept_map
        on concept_map.taxonomy = facts.taxonomy and concept_map.concept = facts.concept

),

with_acceptance as (

    select
        mapped.cik,
        mapped.period_start,
        mapped.period_end,
        mapped.form,
        mapped.accession,
        mapped.unit,
        mapped.value,
        mapped.source_taxonomy,
        mapped.source_concept,
        mapped.field,
        mapped.concept_rank,
        mapped.kind,
        mapped.unit_family,
        coalesce(
            filings.acceptance_ts,
            timezone('America/New_York', mapped.filed + interval '23:59:59')
        ) as acceptance_ts,
        coalesce(filings.ts_imputed, true) as ts_imputed
    from mapped
    left join {{ source('raw', 'filings') }} as filings
        on filings.accession = mapped.accession

)

select * from with_acceptance
