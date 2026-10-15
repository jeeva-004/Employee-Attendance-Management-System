# REVIEW.md

Defects found in the starter `app/main.py`. "Notice" gives a concrete failing input or symptom.

## Helpers

### 1. `compute_late_minutes`
- **What is wrong:** Shift start is built with `punch_in.replace(hour, minute)` in the punch's own time zone (UTC or naive), not IST.
- **How I notice it:** Punch-in `04:10:01Z` (09:40:01 IST) against a 09:30 shift returns 0, should be 10.
- **How I fixed it:** Convert to IST first; shift start is an IST-aware datetime from `shift_window`.

### 2. `compute_late_minutes`
- **What is wrong:** Floors to whole minutes *before* testing `> 10`, so the grace check is on minutes, not on "strictly more than 10:00".
- **How I notice it:** 09:40:01 returns 0; R2 says 10.
- **How I fixed it:** Compare elapsed seconds `> 600`, then floor to minutes.

### 3. `compute_late_minutes`, `compute_overtime`
- **What is wrong:** Overnight shifts ignored: shift start/end always taken from the punch's own calendar day (R1).
- **How I notice it:** 22:00-06:00 shift, punch-out 06:40 next day returns 1480 overtime minutes, should be 40.
- **How I fixed it:** New `shift_window()` returns the attendance date plus IST start and end, with the end on the next day for overnight shifts.

### 4. `compute_work_hours`
- **What is wrong:** Python `round()` is not half-up (binary floats plus banker's rounding).
- **How I notice it:** 9630 s (2.675 h) returns 2.67, should be 2.68.
- **How I fixed it:** `Decimal` division with `ROUND_HALF_UP`.

### 5. `compute_overtime`
- **What is wrong:** No 30-minute minimum; any positive gap is returned.
- **How I notice it:** Punch-out 10 min after shift end returns 10, should be 0.
- **How I fixed it:** Floor to minutes, return it only when `>= 30` (R3).

### 6. `compute_overtime`
- **What is wrong:** Subtracts an aware IST datetime from a punch time that was read from Mongo as naive.
- **How I notice it:** `TypeError: can't subtract offset-naive and offset-aware datetimes`, a 500.
- **How I fixed it:** Client is `tz_aware=True` (see 7) and all datetimes are aware.

### 7. Mongo client setup
- **What is wrong:** Client is not `tz_aware`, so stored UTC instants come back naive. The code then guesses a time zone.
- **How I notice it:** Any arithmetic mixing stored and IST values is wrong or raises.
- **How I fixed it:** `MongoClient(..., tz_aware=True)`; conversion at the API boundary only.

### 8. Whole file
- **What is wrong:** No second truncation (R1). Milliseconds are stored and returned.
- **How I notice it:** `punched_at=...900` stores `.900` instead of `:00`.
- **How I fixed it:** `to_dt()` uses `ms // 1000`, so the truncated value is what is stored and returned.

### 9. Whole file
- **What is wrong:** No indexes are created at startup.
- **How I notice it:** `explain` shows `COLLSCAN`; duplicates are possible (see 12, 20).
- **How I fixed it:** `ensure_indexes()` in the lifespan hook, idempotent. Unique `emp_code`; unique `(emp_code, date)`; `(date desc, emp_code)`; `(status, date desc, emp_code)`; plus two employee indexes for department queries.

## Endpoints

### 1. `GET /health`
- **What is wrong:** Always returns ok, never pings MongoDB.
- **How I notice it:** Stop MongoDB: still 200.
- **How I fixed it:** `ping`, return 503 on `PyMongoError`.

### 2. `EmployeeIn`
- **What is wrong:** No validation at all.
- **How I notice it:** `emp_code="x"`, email `"abc"`, `shift_start="25:99"`, `joined_on="2026-13-45"` and `shift_start == shift_end` are all accepted.
- **How I fixed it:** Pattern and length constraints per the contract, `shift_start != shift_end`, and a real calendar-date check.

### 3. `POST /employees`
- **What is wrong:** Check-then-insert race and no unique index.
- **How I notice it:** Parallel creates with one `emp_code` give several 201s and duplicate rows.
- **How I fixed it:** Unique index; catch `DuplicateKeyError` and return 409.

### 4. `POST /employees`
- **What is wrong:** `created_at` is naive local `datetime.now()` and is returned as a datetime.
- **How I notice it:** Value depends on server time zone; the response is not epoch ms.
- **How I fixed it:** UTC, millisecond precision, serialised as epoch ms.

### 5. `GET /employees`
- **What is wrong:** `skip = page * page_size`.
- **How I notice it:** `page=1` skips the first 20 employees.
- **How I fixed it:** `(page - 1) * page_size`.

### 6. `GET /employees`
- **What is wrong:** `total` counts the whole collection, ignoring `department`.
- **How I notice it:** `?department=Sales` returns 2 items but `total` is every employee.
- **How I fixed it:** `count_documents(q)` with the same filter.

### 7. `GET /employees`
- **What is wrong:** No sort, so pages are unstable. No bounds on `page`/`page_size`.
- **How I notice it:** The contract requires `emp_code` ascending. `page=0` or negative gives a negative skip and a 500. `page_size=0` removes the limit.
- **How I fixed it:** `sort("emp_code")`; `page >= 1`, `1 <= page_size <= 100`.

### 8. `POST /attendance/punch-in`
- **What is wrong:** Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises.
- **How I notice it:** Unknown `emp_code` returns a 500; the contract says 404.
- **How I fixed it:** Explicit 404.

### 9. `POST /attendance/punch-in`
- **What is wrong:** `datetime.fromtimestamp(ms/1000)` uses the server's local zone and `.date()` is a local date. No overnight rule (R1).
- **How I notice it:** A UTC server at 23:00 UTC (04:30 IST next day) records the wrong date; a 02:00 IST punch on a 22:00 shift gets the wrong day.
- **How I fixed it:** Aware UTC instant, date and shift from `shift_window()` in IST.

### 10. `POST /attendance/punch-in`
- **What is wrong:** `if body.punched_at` treats 0 as missing. `punched_at` is an unbounded `int`: lax parsing accepts `"1783312500000"` and `1.0`, and seconds-style values.
- **How I notice it:** `punched_at=0` silently uses "now" instead of 422.
- **How I fixed it:** `StrictInt` bounded 1e11 to 4102444800000, `is not None` check.

### 11. `POST /attendance/punch-in`
- **What is wrong:** Check-then-insert race.
- **How I notice it:** Two simultaneous requests both pass `find_one`, giving two 201s.
- **How I fixed it:** Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409.

### 12. `POST /attendance/punch-in`
- **What is wrong:** `status` is a free string.
- **How I notice it:** `status="ABSENT"` or `"banana"` is accepted.
- **How I fixed it:** `Literal["PRESENT", "WFH", "ON_DUTY"]`, otherwise 422.

### 13. `POST /attendance/punch-in`, `GET /attendance`
- **What is wrong:** Response leaks `id` (the `_id`) and returns raw datetimes.
- **How I notice it:** Contract forbids `_id`; instants must be epoch ms.
- **How I fixed it:** `attendance_out()` builds the exact contract shape.

### 14. `GET /attendance`
- **What is wrong:** Loads the whole collection, sorts in Python, paginates by slicing.
- **How I notice it:** About 100k docs: slow and memory-heavy; no index is used.
- **How I fixed it:** Server-side `sort/skip/limit` backed by indexes; `estimated_document_count()` when unfiltered.

### 15. `GET /attendance`
- **What is wrong:** Sorts by `date` only.
- **How I notice it:** Contract: `date` desc, then `emp_code` asc; ties come back in arbitrary order.
- **How I fixed it:** `sort([("date", -1), ("emp_code", 1)])`.

### 16. `GET /attendance`
- **What is wrong:** No input validation.
- **How I notice it:** `date_from > date_to`, bad dates and unknown `status` are accepted; `page_size=100000` works.
- **How I fixed it:** 422 checks and bounds.

### 17. `GET /attendance`
- **What is wrong:** Legacy docs lacking `half_day`/`history` are returned as-is.
- **How I notice it:** The response schema requires both fields.
- **How I fixed it:** `attendance_out()` defaults them to `false` and `[]`.

## Looked at and judged not a defect

* `load_dotenv()` does not override real environment variables by default, which is what the contract requires.
* `$gte`/`$lte` on `YYYY-MM-DD` strings is correct (lexicographic order equals date order) and the bounds are inclusive.
* Default shift times (09:30 / 18:30) and the punch-in initial values (`punch_out` null, `overtime_minutes` 0, `half_day` false, `history` `[]`) match the contract.
* `status_code=201` on both create endpoints is correct.
* The `localhost` fallback for `MONGO_URI` is acceptable because the environment values take priority. I would remove it for production.