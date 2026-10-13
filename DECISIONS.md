# DECISIONS.md

1. **Indexes.**
We created `uq_emp_code` (unique on `emp_code`) to guarantee employee code uniqueness; `ix_dept_joined` on `(department, joined_on)` for department filtering and headcount ranges; `uq_emp_date` (unique compound index on `(emp_code, date)`) to enforce single daily records and prevent punch-in races; `ix_date_emp` on `(date DESC, emp_code ASC)` for sorted list pagination; and `ix_date_late` on `(date, late_minutes)` for leaderboard filtering. We considered indexing `punch_in` directly but rejected it because queries filter by calendar `date`, making it redundant.

2. **Punch-in race.**
Both requests validate the employee, calculate the same attendance date, and attempt `insert_one`. The compound unique index on `(emp_code, date)` guarantees atomic serialization. One insert commits and returns `201 Created`. The concurrent insert triggers a `DuplicateKeyError`, which our exception handler intercepts to return `409 Conflict`.

3. **Ties.**
We use `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4). The filter `rank <= limit` applies after assigning ranks. If employees tie at the boundary rank, all tied candidates are included and deterministically ordered by `_id` (`emp_code`), ensuring fair cutoff behavior.

4. **Headcount.**
We run an aggregation directly on `employees` filtered by `joined_on <= month_end` grouped by department. We merge log metrics onto this complete roster, ensuring employees with zero logs are fully counted with `0` present days.

5. **One thing you would change for 100x data.**
I would replace on-the-fly aggregations over raw attendance logs with pre-aggregated daily/monthly rollups maintained asynchronously via MongoDB Change Streams into dedicated summary collections.
