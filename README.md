# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All 12 endpoints in `openapi.yaml` are implemented in `app/main.py`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"        # or put MONGO_URI / MONGO_DB in a local .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup. `GET /health` returns 503 when MongoDB is unreachable.

## Notes and assumptions

- Dates and shifts are interpreted in IST; instants are stored as UTC datetimes and returned as epoch milliseconds.
- Analytics are MongoDB aggregation pipelines. Final rounding is half-up and done in Python, because MongoDB's `$round` rounds half to even.
- Each pipeline is pinned to an index with a hint so the explain plan is always an index scan; the explain endpoint uses the same builders and hints as the real endpoints.
- Where the spec is silent:
  - Punch-out checks "already punched out" (409) before the time checks (422).
  - A PATCH that supplies identical status and punch times counts as "changes nothing" (422); `null` values are rejected.
  - The 92-day trend limit counts inclusive days (92 rows allowed, 93 rejected).
  - The 7-day moving average uses unrounded daily rates.
  - Logs dated before an employee's `joined_on` are still counted in sums.
- See `REVIEW.md` for the defects found and fixed in the starter code.
