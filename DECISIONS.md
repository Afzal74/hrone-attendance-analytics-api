# Decisions

1. **Indexes.** Unique indexes on `employees.emp_code` and `attendance_logs(emp_code, date)` enforce employee identity and one daily record. `(date, emp_code)` supports attendance listing; `(department, emp_code)`, `(department, joined_on)`, and `(joined_on)` support employee filters and join-date queries; `(emp_code, date, status)` supports employee/date/status lookups. `(emp_code, punch_in DESC)` supports selecting the latest eligible punch-in. I avoided indexing every derived metric because extra indexes increase write cost.

2. **Punch races.** Punch-in attempts for the same employee/date contend on the unique key; one insert succeeds, and duplicate-key errors become 409. Punch-out reads the latest eligible record, rejects it if already closed, then conditionally updates only when `punch_out` is null; a concurrent loser gets 409. Regularization atomically compares prior history, updates fields, and appends audit history; stale requests return 409.

3. **Ties.** MongoDB `$rank` ranks by `total_late_minutes`; ties share a rank and the next rank is skipped (1, 2, 2, 4). Applying `rank <= limit` after ranking includes cutoff ties; final order is late minutes descending, then `emp_code` ascending.

4. **Headcount.** Department summary starts from employees who joined by period end, then looks up attendance, so employees without logs still count.

5. **100× data.** Maintain daily rollups while retaining raw logs for audit, reducing request-time aggregation cost.
