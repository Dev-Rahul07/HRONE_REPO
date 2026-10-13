# REVIEW.md

List of defects identified and resolved in the starter implementation of `app/main.py`:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `lifespan` / `ensure_indexes` | No indexes defined on startup; race conditions and duplicate entries allowed. | Simultaneous duplicate employee creation or multiple punch-ins succeeded. | Added idempotent startup indexes: unique `emp_code`, compound unique `(emp_code, date)`, and query indexes. |
| 2 | MongoDB client initialization | Naive datetimes returned by default PyMongo, corrupting epoch conversions. | Inconsistent timestamps across UTC/local machine environments. | Configured `MongoClient(MONGO_URI, tz_aware=True)` and exact integer epoch millisecond math. |
| 3 | `truncate_seconds` / R1 | Instants retained millisecond fractions prior to calculation. | Input like `09:40:00.800` affected boundary calculations. | Explicitly truncated timestamps using `.replace(microsecond=0)`. |
| 4 | `calc_late_minutes` / R2 | Grace period checked `<= 10` min or counted minutes from grace end instead of shift start. | Punch at `09:40:01` counted as 0 or 1 min instead of 10 min. | Enforced strict `secs > 600` check and computed floor minutes from `shift_start`. |
| 5 | `round_half_up` / `calc_work_hours` / R4, R8 | Built-in Python `round()` uses Banker's rounding (rounds half to even). | Values like `2.5` or `4.425` rounded unexpectedly to even numbers. | Implemented custom rounding using `Decimal.quantize(..., rounding=ROUND_HALF_UP)`. |
| 6 | `attendance_date_of` / R1 | Overnight shifts (e.g., 22:00-06:00) mapped early morning punches to the next day. | Punch-in at `01:00` on July 15 recorded as July 15 instead of shift start on July 14. | Decremented calendar date by 1 day when `shift_end <= shift_start` and time `< shift_end`. |
| 7 | `punch_out` | Concurrent punch-outs caused race conditions and non-atomic state updates. | Two concurrent requests both returned 200 with differing punch-out times. | Implemented atomic `find_one_and_update` with filter `punch_out: None`, returning 409 if already closed. |
| 8 | `regularize` | Missing optimistic concurrency and audit logging; allowed silent overwrites. | Concurrent edits overwrote history entries without conflict detection. | Implemented compare-and-swap (CAS) matching current `history` state and appended structured audit delta. |
| 9 | `employee_monthly` / R7 | Counted working days from start of month even for mid-month joiners. | Employee joining mid-month received inflated working day total and deflated attendance %. | Counted working days starting from `max(first_day_of_month, joined_on)`. |
| 10 | `department_summary` / R9 | Aggregated only over `attendance_logs`, dropping employees with zero logs. | Employees with no logs for the month were missing from department headcount. | Aggregated total headcount from `employees` (`joined_on <= last_day`) and merged with log metrics. |
| 11 | `late_leaderboard` | Naive sorting and slicing failed standard competition ranking for ties. | Tied employees at cutoff received inconsistent ranks or improper skips (e.g. 1, 2, 3 instead of 1, 2, 2, 4). | Utilized MongoDB `$setWindowFields` with `$rank` and applied limit after ranking. |
| 12 | `department_trend` | Calendar days with zero attendance were missing from the date sequence. | Gaps in trend timeline; weekends and zero-attendance days omitted. | Applied MongoDB `$densify` stage across date bounds and computed 7-day trailing moving average. |

---

### What was examined and confirmed NOT to be a defect:
1. **Exclusion of `_id` from API payloads:** MongoDB internal `ObjectId` is intentionally hidden. The contract specifies `emp_code` and `(emp_code, date)` as the canonical business identifiers.
2. **Absence of punch-in creation for `ABSENT` / `LEAVE`:** By specification, punch endpoints only handle presence statuses (`PRESENT`, `WFH`, `ON_DUTY`). `ABSENT` and `LEAVE` are seeded or updated via regularization.
3. **Unrecorded weekdays:** A weekday without an attendance record is intentionally treated as unrecorded rather than automatically marking the employee `ABSENT`.
