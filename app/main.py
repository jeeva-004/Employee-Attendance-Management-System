# Employee Attendance & Analytics API

import calendar
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls, datetime, time as dtime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from bson import json_util
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
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


# Startup: indexes (idempotent)
def ensure_indexes():
    try:
        # employees
        db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
        db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code")
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="dept_joined")
        db.employees.create_index([("joined_on", ASCENDING)], name="joined")
        # attendance_logs
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
        db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp")
        db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in")
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


# Helpers: time conversion
def to_dt(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return datetime.fromtimestamp(ms // 1000, tz=UTC)


def now_dt() -> datetime:
    return datetime.fromtimestamp(int(datetime.now(UTC).timestamp()), tz=UTC)


def now_precise() -> datetime:
    n = datetime.now(UTC)
    return n.replace(microsecond=n.microsecond // 1000 * 1000)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)  # exact integer arithmetc


def parse_date(value: str, name: str) -> str:
    if not DATE_RE.match(value):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        date_cls.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid calendar date")
    return value


# Helpers: business rules R1-R5
def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def window_for_date(day: str, shift_start: str, shift_end: str):
    """IST shift start/end datetimes for a given attendance date (end is next day for overnight shifts)."""
    d = date_cls.fromisoformat(day)
    s, e = _hm(shift_start), _hm(shift_end)
    end_day = d + timedelta(days=1) if e <= s else d
    return datetime.combine(d, s, tzinfo=IST), datetime.combine(end_day, e, tzinfo=IST)


def shift_window(punch_in: datetime, shift_start: str, shift_end: str):
    """R1: returns (attendance_date 'YYYY-MM-DD', shift_start_dt IST, shift_end_dt IST)."""
    local = punch_in.astimezone(IST)
    day = local.date()
    if _hm(shift_end) <= _hm(shift_start) and local.time() < _hm(shift_end):
        day -= timedelta(days=1)  # belongs to the shift that day before
    start, end = window_for_date(day.isoformat(), shift_start, shift_end)
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


# Helpers: serialisation (Mongo document -> API shape)
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


# Models
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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")  # derived fields can not be sent

    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_nulls(self):
        for f in ("status", "punch_in", "punch_out"):
            if f in self.model_fields_set and getattr(self, f) is None:
                raise ValueError(f"{f} must not be null")
        return self


# Endpoints
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


ATTENDANCE_SORT = {"date": -1, "emp_code": 1}


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
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
    return q


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    col = db.attendance_logs
    total = col.count_documents(q) if q else col.estimated_document_count()
    cur = (
        col.find(q, {"_id": 0})
        .sort(list(ATTENDANCE_SORT.items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = to_dt(body.punched_at) if body.punched_at is not None else now_dt()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    if ts <= rec["punch_in"]:
        raise HTTPException(422, "punched_at must be after punch_in")
    if ts - rec["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    _, end = window_for_date(rec["date"], emp["shift_start"], emp["shift_end"])
    work_hours = compute_work_hours(rec["punch_in"], ts)
    derived = {
        "punch_out": ts,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(ts, end),
        "half_day": compute_half_day(work_hours),
    }
    # only one concurrent request can flip punch_out from null; the loser matches nothing -> 409.
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"]}, {"$set": derived}
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return attendance_out({**rec, **derived})


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for this date")

    # proposed final state
    status = body.status or rec["status"]
    old_in, old_out = rec.get("punch_in"), rec.get("punch_out")
    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"{status} clears punch times; do not supply punch_in/punch_out")
        new_in = new_out = None
    else:
        new_in = to_dt(body.punch_in) if body.punch_in is not None else old_in
        new_out = to_dt(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")

    if new_in is not None:
        if shift_window(new_in, emp["shift_start"], emp["shift_end"])[0] != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            if new_out <= new_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if new_out - new_in > timedelta(hours=24):
                raise HTTPException(422, "punch_out is more than 24 hours after punch_in")

    primary_changed = (
        status != rec["status"] or to_ms(new_in) != to_ms(old_in) or to_ms(new_out) != to_ms(old_out)
    )
    if not primary_changed:
        raise HTTPException(422, "request changes nothing")

    # recompute derived fields (R2-R5)
    start, end = window_for_date(date, emp["shift_start"], emp["shift_end"])
    late = compute_late_minutes(new_in, start) if new_in else 0
    if new_in and new_out:
        work_hours = compute_work_hours(new_in, new_out)
        overtime, half_day = compute_overtime(new_out, end), compute_half_day(work_hours)
    else:
        work_hours, overtime, half_day = None, 0, False
    new = {
        "status": status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    old = {
        "status": rec["status"], "punch_in": old_in, "punch_out": old_out, "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0, "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {}
    for f, nv in new.items():
        same = to_ms(nv) == to_ms(old[f]) if f in ("punch_in", "punch_out") else nv == old[f]
        if not same:
            changes[f] = {"from": old[f], "to": nv}
    entry = {"at": now_precise(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # optimistic concurrency: write only if the record is exactly as we read it
    n_hist = len(rec.get("history") or [])
    res = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "status": rec["status"],
            "punch_in": old_in,
            "punch_out": old_out,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, n_hist]},
        },
        {"$set": new, "$push": {"history": entry}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently; retry")
    return attendance_out({**rec, **new, "history": list(rec.get("history") or []) + [entry]})


# Analytics: pipeline builders (shared with the explain endpoint) + endpoints
MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MonthQ = Annotated[str, Query(pattern=MONTH_PATTERN)]
WEEKDAYS = [2, 3, 4, 5, 6]  # $dayOfWeek: 1 = Sunday ... 7 = Saturday
_DATE = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}
_IS_PRESENT = {"$in": ["$status", list(PRESENCE_STATUSES)]}
_UNIT = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}  # R5: half day = 0.5; missing half_day = false
PRESENT_WEEKDAY = {"$cond": [{"$and": [_IS_PRESENT, {"$in": [{"$dayOfWeek": _DATE}, WEEKDAYS]}]}, _UNIT, 0]}  # R7
PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _UNIT, 0]}
IS_LATE = {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def agg_hint(name: str, department: Optional[str] = None) -> str:
    """Index each pipeline is pinned to, so its first stage is always an IXSCAN (endpoint and explain agree)."""
    return {
        "employee_monthly": "uq_emp_date",
        "department_summary": "dept_joined" if department is not None else "joined",
        "late_leaderboard": "date_emp",
        "department_trend": "dept_emp_code",
    }[name]


def month_bounds(month: str):
    y, mo = int(month[:4]), int(month[5:7])
    if y < 1:
        raise HTTPException(422, "invalid month")
    return date_cls(y, mo, 1), date_cls(y, mo, calendar.monthrange(y, mo)[1])


def count_working_days(start: date_cls, end: date_cls) -> int:
    n, d = 0, start
    while d <= end:
        n += d.weekday() < 5  # Mon-Fri, no holidays (R7)
        d += timedelta(days=1)
    return n


def pl_employee_monthly(emp_code: str, first: str, last: str):
    """P1: one employee's month. Runs on attendance_logs."""
    return "attendance_logs", [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": PRESENT_WEEKDAY},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": IS_LATE},
            "total_late_minutes": {"$sum": "$late_minutes"},
            "total_overtime_minutes": {"$sum": "$overtime_minutes"},
        }},
    ]


def pl_department_summary(first: str, last: str, department: Optional[str] = None):
    """P2: per-department month summary. Starts from employees so people with zero logs still count (R9)."""
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    return "employees", [
        {"$match": match},
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first, "$lte": last}}},
                {"$group": {  # one row per employee, so headcount below stays one-per-employee
                    "_id": None,
                    "present_days": {"$sum": PRESENT_WEEKDAY},
                    "late_count": {"$sum": IS_LATE},
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                    "wh_sum": {"$sum": {"$cond": [HAS_HOURS, "$work_hours", 0]}},
                    "wh_n": {"$sum": {"$cond": [HAS_HOURS, 1, 0]}},
                }},
            ],
            "as": "agg",
        }},
        {"$unwind": {"path": "$agg", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$agg.present_days"},
            "late_count": {"$sum": "$agg.late_count"},
            "total_late_minutes": {"$sum": "$agg.total_late_minutes"},
            "leave_count": {"$sum": "$agg.leave_count"},
            "on_duty_count": {"$sum": "$agg.on_duty_count"},
            "wh_sum": {"$sum": "$agg.wh_sum"},
            "wh_n": {"$sum": "$agg.wh_n"},
        }},
        {"$sort": {"_id": 1}},
    ]


def pl_late_leaderboard(first: str, last: str, limit: int, department: Optional[str] = None):
    """P3: late leaderboard with standard competition ranking ($rank). Runs on attendance_logs."""
    stages = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs whose employee does not exist are dropped
    ]
    if department is not None:
        stages.append({"$match": {"emp.department": department}})  # rank inside the department only
    stages += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return "attendance_logs", stages


def pl_department_trend(department: str, d_from: date_cls, d_to: date_cls):
    """P4: one row per calendar day (gap-filled inside the pipeline) + 7-row moving average. Runs on employees."""
    n_days = (d_to - d_from).days + 1
    start = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    return "employees", [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "codes": {"$push": "$emp_code"}, "joined": {"$push": "$joined_on"}}},
        {"$set": {"day": {"$map": {
            "input": {"$range": [0, n_days]}, "as": "i",
            "in": {"$add": [start, {"$multiply": ["$$i", 86400000]}]},
        }}}},
        {"$unwind": "$day"},
        {"$set": {"ds": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        {"$lookup": {
            "from": "attendance_logs", "localField": "ds", "foreignField": "date", "let": {"c": "$codes"},
            "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$c"]}}},
                {"$group": {"_id": None, "present": {"$sum": PRESENT_ANY_DAY}, "late": {"$sum": IS_LATE}}},
            ],
            "as": "a",
        }},
        {"$set": {
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]},
            "headcount": {"$size": {"$filter": {"input": "$joined", "as": "j", "cond": {"$lte": ["$$j", "$ds"]}}}},
            "present_count": {"$ifNull": [{"$arrayElemAt": ["$a.present", 0]}, 0]},
            "late_count": {"$ifNull": [{"$arrayElemAt": ["$a.late", 0]}, 0]},
        }},
        {"$set": {"rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": ["$present_count", "$headcount"]}, None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"ds": 1}, "output": {"ma": {"$avg": "$rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": "$ds", "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "rate": 1, "ma": 1,
        }},
        {"$sort": {"date": 1}},
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQ):
    first, last = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if not emp:
        raise HTTPException(404, "employee not found")
    coll, pl = pl_employee_monthly(emp_code, first.isoformat(), last.isoformat())
    agg = next(iter(db[coll].aggregate(pl, hint=agg_hint("employee_monthly"))), {})
    working = count_working_days(max(first, date_cls.fromisoformat(emp["joined_on"])), last)
    present = agg.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": half_up(present, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": half_up(Decimal(str(present)) * 100 / Decimal(working), 2) if working else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQ, department: Optional[str] = None):
    first, last = month_bounds(month)
    coll, pl = pl_department_summary(first.isoformat(), last.isoformat(), department)
    items = []
    for r in db[coll].aggregate(pl, hint=agg_hint("department_summary", department)):
        n = int(r.get("wh_n", 0))
        # work_hours have 2 decimals, so snap the double sum before an exact Decimal division
        avg = half_up(Decimal(str(round(r.get("wh_sum", 0), 2))) / n, 2) if n else None
        items.append({
            "department": r["_id"],
            "headcount": int(r["headcount"]),
            "present_days": half_up(r.get("present_days", 0), 2),
            "avg_work_hours": avg,
            "late_count": int(r.get("late_count", 0)),
            "total_late_minutes": int(r.get("total_late_minutes", 0)),
            "leave_count": int(r.get("leave_count", 0)),
            "on_duty_count": int(r.get("on_duty_count", 0)),
        })
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQ, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    coll, pl = pl_late_leaderboard(first.isoformat(), last.isoformat(), limit, department)
    items = [
        {
            "rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"], "department": r["department"],
            "total_late_minutes": int(r["total_late_minutes"]), "late_count": int(r["late_count"]),
        }
        for r in db[coll].aggregate(pl, hint=agg_hint("late_leaderboard"))
    ]
    return {"month": month, "items": items}


def parse_trend_range(from_: str, to: str):
    d_from = date_cls.fromisoformat(parse_date(from_, "from"))
    d_to = date_cls.fromisoformat(parse_date(to, "to"))
    if d_to < d_from:
        raise HTTPException(422, "to must not be before from")
    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(422, "range must not exceed 92 days")
    return d_from, d_to


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    d_from, d_to = parse_trend_range(from_, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "unknown department")
    coll, pl = pl_department_trend(department, d_from, d_to)
    items = []
    for r in db[coll].aggregate(pl, hint=agg_hint("department_trend")):
        hc = int(r["headcount"])
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": hc,
            "present_count": half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": half_up(Decimal(str(r["present_count"])) / hc, 4) if r.get("rate") is not None else None,
            "moving_avg_7d": half_up(r["ma"], 4) if r.get("ma") is not None else None,
        })
    return {"department": department, "items": items}


# Admin: explain
ExplainTarget = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, "missing query parameter(s): " + ", ".join(missing))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: ExplainTarget,
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=MONTH_PATTERN)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: PageQ = 1,
    page_size: PageSizeQ = 20,
):
    """executionStats explain of the endpoint's main query, built with the same helpers the endpoint uses."""
    if endpoint == "attendance_list":
        coll = "attendance_logs"
        cmd = {
            "find": coll,
            "filter": attendance_filter(emp_code, date_from, date_to, status),
            "sort": ATTENDANCE_SORT,
            "projection": {"_id": 0},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    else:
        hint_dept = None
        if endpoint == "employee_monthly":
            _need(emp_code=emp_code, month=month)
            first, last = month_bounds(month)
            coll, pl = pl_employee_monthly(emp_code, first.isoformat(), last.isoformat())
        elif endpoint == "department_summary":
            _need(month=month)
            first, last = month_bounds(month)
            coll, pl = pl_department_summary(first.isoformat(), last.isoformat(), department)
            hint_dept = department
        elif endpoint == "late_leaderboard":
            _need(month=month)
            first, last = month_bounds(month)
            coll, pl = pl_late_leaderboard(first.isoformat(), last.isoformat(), limit, department)
        else:  # department_trend
            _need(department=department, **{"from": from_}, to=to)
            d_from, d_to = parse_trend_range(from_, to)
            coll, pl = pl_department_trend(department, d_from, d_to)
        cmd = {"aggregate": coll, "pipeline": pl, "cursor": {}, "hint": agg_hint(endpoint, hint_dept)}
    raw = db.command({"explain": cmd, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}