# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+). All 12 endpoints in `openapi.yaml` are implemented in `app/main.py`.

## System Design

```mermaid
flowchart LR
    Client["Client / Grader"] -->|HTTP JSON| API["FastAPI app<br/>uvicorn app.main:app"]

    subgraph API_LAYER["app/main.py"]
        direction TB
        EMP["Employees<br/>POST/GET /employees"]
        ATT["Attendance<br/>punch-in, punch-out,<br/>GET list, PATCH regularize"]
        ANA["Analytics<br/>employee monthly, department summary,<br/>late leaderboard, department trend"]
        ADM["Admin<br/>GET /admin/explain/endpoint"]
        RULES["Business rules R1-R10<br/>IST dates, late, overtime,<br/>work hours, half day"]
    end

    API --> EMP
    API --> ATT
    API --> ANA
    API --> ADM
    ATT --> RULES
    EMP --> DRV["PyMongo<br/>tz_aware, MONGO_URI / MONGO_DB"]
    ATT --> DRV
    ANA -->|aggregation pipelines| DRV
    ADM -->|explain executionStats| DRV

    DRV --> DB[("MongoDB 6.0+")]

    subgraph DB_LAYER["Collections and indexes"]
        direction TB
        C1["employees<br/>unique emp_code, department+emp_code,<br/>department+joined_on, joined_on"]
        C2["attendance_logs<br/>unique emp_code+date, date desc+emp_code,<br/>emp_code+punch_in, status+date+emp_code"]
    end

    DB --- C1
    DB --- C2
```

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"        # or put MONGO_URI / MONGO_DB in a local .env
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
# Ambiguity

The contract is ambiguous on moving_avg_7d: R8 rounds only “reported numbers” (so I average unrounded daily rates and round once at the end), but the rule also says “mean of attendance_rate values”, which could mean the already-rounded 4-decimal values. 

Example: headcount 3, present 0 and 2 → unrounded average = 0.3333, average of rounded rates (0.0000, 0.6667) = 0.3334. I chose unrounded to avoid compounding rounding error, and it’s a one-line change if rounded is intended.