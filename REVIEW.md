# REVIEW.md

Defects found in the starter `app/main.py`. "Notice" gives a concrete failing input or symptom.

## Helpers

| # | Where | What is wrong | How I notice it | How I fixed it |
|---|-------|---------------|-----------------|----------------|
| 1 | `compute_late_minutes` | Shift start is built with `punch_in.replace(hour, minute)` in the punch's own time zone (UTC or naive), not IST. | Punch-in `04:10:01Z` (09:40:01 IST) against a 09:30 shift returns 0, should be 10. | Convert to IST first; shift start is an IST-aware datetime from `shift_window`. |

| 2 | `compute_late_minutes` | Floors to whole minutes *before* testing `> 10`, so the grace check is on minutes, not on "strictly more than 10:00". | 09:40:01 returns 0; R2 says 10. | Compare elapsed seconds `> 600`, then floor to minutes. |

| 3 | `compute_late_minutes`, `compute_overtime` | Overnight shifts ignored: shift start/end always taken from the punch's own calendar day (R1). | 22:00-06:00 shift, punch-out 06:40 next day returns 1480 overtime minutes, should be 40. | New `shift_window()` returns the attendance date plus IST start and end, with the end on the next day for overnight shifts. |

| 4 | `compute_work_hours` | Python `round()` is not half-up (binary floats plus banker's rounding). | 9630 s (2.675 h) returns 2.67, should be 2.68. | `Decimal` division with `ROUND_HALF_UP`. |

| 5 | `compute_overtime` | No 30-minute minimum; any positive gap is returned. | Punch-out 10 min after shift end returns 10, should be 0. | Floor to minutes, return it only when `>= 30` (R3). |

| 6 | `compute_overtime` | Subtracts an aware IST datetime from a punch time that was read from Mongo as naive. | `TypeError: can't subtract offset-naive and offset-aware datetimes`, a 500. | Client is `tz_aware=True` (see 7) and all datetimes are aware. |

| 7 | Mongo client setup | Client is not `tz_aware`, so stored UTC instants come back naive. The code then guesses a time zone. | Any arithmetic mixing stored and IST values is wrong or raises. | `MongoClient(..., tz_aware=True)`; conversion at the API boundary only. |

| 8 | Whole file | No second truncation (R1). Milliseconds are stored and returned. | `punched_at=...900` stores `.900` instead of `:00`. | `to_dt()` uses `ms // 1000`, so the truncated value is what is stored and returned. |

| 9 | Whole file | No indexes are created at startup. | `explain` shows `COLLSCAN`; duplicates are possible (see 12, 20). | `ensure_indexes()` in the lifespan hook, idempotent. Unique `emp_code`; unique `(emp_code, date)`; `(date desc, emp_code)`; `(status, date desc, emp_code)`; plus two employee indexes for department queries. |



## Endpoints

| # | Where | What is wrong | How I notice it | How I fixed it |
|---|-------|---------------|-----------------|----------------|

| 1 | `GET /health` | Always returns ok, never pings MongoDB. | Stop MongoDB: still 200. | `ping`, return 503 on `PyMongoError`. |

| 2 | `EmployeeIn` | No validation at all. | `emp_code="x"`, email `"abc"`, `shift_start="25:99"`, `joined_on="2026-13-45"` and `shift_start == shift_end` are all accepted. | Pattern and length constraints per the contract, `shift_start != shift_end`, and a real calendar-date check. |

| 3 | `POST /employees` | Check-then-insert race and no unique index. | Parallel creates with one `emp_code` give several 201s and duplicate rows. | Unique index; catch `DuplicateKeyError` and return 409. |

| 4 | `POST /employees` | `created_at` is naive local `datetime.now()` and is returned as a datetime. | Value depends on server time zone; the response is not epoch ms. | UTC, millisecond precision, serialised as epoch ms. |

| 5 | `GET /employees` | `skip = page * page_size`. | `page=1` skips the first 20 employees. | `(page - 1) * page_size`. |

| 6 | `GET /employees` | `total` counts the whole collection, ignoring `department`. | `?department=Sales` returns 2 items but `total` is every employee. | `count_documents(q)` with the same filter. |

| 7 | `GET /employees` | No sort, so pages are unstable. No bounds on `page`/`page_size`. | The contract requires `emp_code` ascending. `page=0` or negative gives a negative skip and a 500. `page_size=0` removes the limit. | `sort("emp_code")`; `page >= 1`, `1 <= page_size <= 100`. |

| 8 | `POST /attendance/punch-in` | Unknown employee: `emp` is `None`, then `emp["shift_start"]` raises. | Unknown `emp_code` returns a 500; the contract says 404. | Explicit 404. |

| 9 | `POST /attendance/punch-in` | `datetime.fromtimestamp(ms/1000)` uses the server's local zone and `.date()` is a local date. No overnight rule (R1). | A UTC server at 23:00 UTC (04:30 IST next day) records the wrong date; a 02:00 IST punch on a 22:00 shift gets the wrong day. | Aware UTC instant, date and shift from `shift_window()` in IST. |

| 10 | `POST /attendance/punch-in` | `if body.punched_at` treats 0 as missing. `punched_at` is an unbounded `int`: lax parsing accepts `"1783312500000"` and `1.0`, and seconds-style values. | `punched_at=0` silently uses "now" instead of 422. | `StrictInt` bounded 1e11 to 4102444800000, `is not None` check. |

| 11 | `POST /attendance/punch-in` | Check-then-insert race. | Two simultaneous requests both pass `find_one`, giving two 201s. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |

| 12 | `POST /attendance/punch-in` | `status` is a free string. | `status="ABSENT"` or `"banana"` is accepted. | `Literal["PRESENT", "WFH", "ON_DUTY"]`, otherwise 422. |

| 13 | `POST /attendance/punch-in`, `GET /attendance` | Response leaks `id` (the `_id`) and returns raw datetimes. | Contract forbids `_id`; instants must be epoch ms. | `attendance_out()` builds the exact contract shape. |

| 14 | `GET /attendance` | Loads the whole collection, sorts in Python, paginates by slicing. | About 100k docs: slow and memory-heavy; no index is used. | Server-side `sort/skip/limit` backed by indexes; `estimated_document_count()` when unfiltered. |

| 15 | `GET /attendance` | Sorts by `date` only. | Contract: `date` desc, then `emp_code` asc; ties come back in arbitrary order. | `sort([("date", -1), ("emp_code", 1)])`. |

| 16 | `GET /attendance` | No input validation. | `date_from > date_to`, bad dates and unknown `status` are accepted; `page_size=100000` works. | 422 checks and bounds. |

| 17 | `GET /attendance` | Legacy docs lacking `half_day`/`history` are returned as-is. | The response schema requires both fields. | `attendance_out()` defaults them to `false` and `[]`. |

## Looked at and judged not a defect

* `load_dotenv()` does not override real environment variables by default, which is what the contract requires.
* `$gte`/`$lte` on `YYYY-MM-DD` strings is correct (lexicographic order equals date order) and the bounds are inclusive.
* Default shift times (09:30 / 18:30) and the punch-in initial values (`punch_out` null, `overtime_minutes` 0, `half_day` false, `history` `[]`) match the contract.
* `status_code=201` on both create endpoints is correct.
* The `localhost` fallback for `MONGO_URI` is acceptable because environment values take priority. I would remove it for production.