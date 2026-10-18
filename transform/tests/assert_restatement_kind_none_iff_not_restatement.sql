-- restatement_kind is 'none' exactly when is_restatement is false (decision A-25).
select *
from {{ ref('fundamentals_pit') }}
where (restatement_kind = 'none' and is_restatement)
   or (restatement_kind <> 'none' and not is_restatement)
