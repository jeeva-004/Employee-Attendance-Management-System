"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded; real environment variables win)
"""
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False: real environment variables take priority

# tz_aware=True: PyMongo returns aware UTC datetimes, so no naive/aware mixing.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes():
    try:
        # employees
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
        # attendance_logs
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp"
        )
    except PyMongoError as exc:  # keep the app up so /health can report 503
        print(f"[startup] index creation failed: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_dt() -> datetime:
    return datetime.fromtimestamp(int(datetime.now(UTC).timestamp()), tz=UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)  # exact integer arithmetic


def parse_date(value: str, name: str) -> str:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")
    return value


# --------------------------------------------------------------------------- #
# Helpers: business rules R1-R5
# --------------------------------------------------------------------------- #
def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def shift_window(punch_in: datetime, shift_start: str, shift_end: str):
    """R1: returns (attendance_date 'YYYY-MM-DD', shift_start_dt IST, shift_end_dt IST)."""
    local = punch_in.astimezone(IST)
    s, e = _hm(shift_start), _hm(shift_end)
    overnight = e <= s
    day = local.date()
    if overnight and local.time() < e:  # belongs to the shift that started yesterday
        day -= timedelta(days=1)
    start = datetime.combine(day, s, tzinfo=IST)
    end = datetime.combine(day + timedelta(days=1) if overnight else day, e, tzinfo=IST)
    return day.isoformat(), start, end


def half_up(value, places: int = 2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_late_minutes(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals half-up."""
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes past shift end, counted only when >= 30."""
    minutes = int((punch_out - shift_end_dt).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: rounded work_hours < 4.50."""
    return work_hours is not None and work_hours < 4.5


# --------------------------------------------------------------------------- #
# Helpers: serialisation (Mongo document -> API shape)
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _ms_if_dt(v):
    return to_ms(v) if isinstance(v, datetime) else v


def history_out(h: dict) -> dict:
    changes = {
        f: {"from": _ms_if_dt(c.get("from")), "to": _ms_if_dt(c.get("to"))}
        for f, c in (h.get("changes") or {}).items()
    }
    return {"at": to_ms(h.get("at")), "by": h.get("by"), "reason": h.get("reason"), "changes": changes}


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_out(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
PageQ = Annotated[int, Query(ge=1)]
PageSizeQ = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @model_validator(mode="after")
    def _check(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        if not DATE_RE.match(self.joined_on):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date_cls.fromisoformat(self.joined_on)  # ValueError -> 422
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    now = datetime.now(UTC)
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)  # BSON keeps ms
    try:
        db.employees.insert_one(doc)  # unique index decides; no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: PageQ = 1, page_size: PageSizeQ = 20):
    q = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    cur = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    d, start, _ = shift_window(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, start),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index makes this race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None:
        parse_date(date_from, "date_from")
    if date_to is not None:
        parse_date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    col = db.attendance_logs
    total = col.count_documents(q) if q else col.estimated_document_count()
    cur = (
        col.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO (not yet built):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
