# DECISIONS.md

1. **Indexes.** Unique `emp_code` and unique `(emp_code, date)` prevent duplicates and serve per-employee lookups. `(date desc, emp_code)` serves the list sort and month scans; `(status, date desc, emp_code)` the status filter; `(emp_code, punch_in desc)` punch-out; the `department` and `joined_on` indexes the department queries. Rejected: an index on `late_minutes`; the leaderboard groups the whole month anyway.
2. **Punch-in race.** Both requests pass the employee check and reach `insert_one`. The unique `(emp_code, date)` index lets one succeed; the other raises `DuplicateKeyError`, returned as 409. There is no check-then-insert gap.
3. **Ties.** Standard competition ranking via `$rank`; `limit` filters on rank, so every employee tied at the cutoff is returned and the list may exceed `limit`.
4. **Headcount.** The pipeline starts from `employees` and `$lookup`s their logs, so someone with no logs still produces a row and is counted.
5. **100x data.** Precompute daily per-department rollups on each punch, so analytics read small summaries instead of scanning logs.
