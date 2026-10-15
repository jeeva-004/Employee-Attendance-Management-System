# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All 12 endpoints in `openapi.yaml` are implemented in `app/main.py`.

## System Design

```mermaid
flowchart TB
    A["Client / Grader"]
    B["FastAPI app<br/>uvicorn app.main:app"]
    C["Endpoints<br/>Employees · Attendance<br/>Analytics · Admin explain"]
    D["Business rules R1-R10<br/>IST dates, late, overtime,<br/>work hours, half day"]
    E["PyMongo<br/>tz_aware · MONGO_URI / MONGO_DB"]
    F[("MongoDB 6.0+")]
    G["employees<br/>unique emp_code<br/>department indexes"]
    H["attendance_logs<br/>unique emp_code + date<br/>date, status, punch_in indexes"]

    A -->|HTTP JSON| B
    B --> C
    C --> D
    C -->|"queries, aggregations, explain"| E
    D --> E
    E --> F
    F --> G
    F --> H
```

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"        # or put MONGO_URI / MONGO_DB in a local .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```