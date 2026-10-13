"""
Employee Attendance & Analytics API.

Run locally:
    uvicorn app.main:app --port 8000 --reload
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pydantic import BaseModel, Field, field_validator, model_validator

# CONFIG + DATABASE
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

# Keep MongoClient timezone-aware so PyMongo returns UTC datetimes directly,
# ensuring clean and accurate epoch millisecond conversions.
client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]
emp_col = db["employees"]
log_col = db["attendance_logs"]

# ---------------------------------------------------------------------------
# Helpers (time conversions, rounding & calendar utilities)
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))      # Shifts and dates operate in IST
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")


def to_millis(dt: datetime) -> int:
    """Convert datetime to epoch milliseconds without floating-point precision loss."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def from_millis(value: int) -> datetime:
    """Convert epoch milliseconds back to a UTC datetime."""
    return EPOCH + timedelta(milliseconds=int(value))


def truncate_seconds(dt: datetime) -> datetime:
    """Truncate timestamp to whole seconds before performing attendance calculations."""
    return dt.replace(microsecond=0)


def now_utc() -> datetime:
    return truncate_seconds(datetime.now(timezone.utc))


def round_half_up(value, places: int = 2):
    """Round values using standard half-up rounding (avoiding banker's rounding bias)."""
    if value is None:
        return None
    quant = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def month_bounds(month: str):
    """Return first and last date of month string 'YYYY-MM'."""
    y, m = int(month[:4]), int(month[5:7])
    first = date_cls(y, m, 1)
    nxt = date_cls(y + 1, 1, 1) if m == 12 else date_cls(y, m + 1, 1)
    return first, nxt - timedelta(days=1)


def count_working_days(start: date_cls, end: date_cls) -> int:
    """Count Monday-Friday working days between two dates inclusive."""
    if end < start:
        return 0
    n = 0
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon=0 ... Fri=4
            n += 1
        d += timedelta(days=1)
    return n


# ---- Shift & Attendance Date Calculations --------------------------------

def shift_start_dt(att_date: str, shift_start: str) -> datetime:
    """Combine attendance date and shift start time (IST) into an aware datetime."""
    h, m = (int(x) for x in shift_start.split(":"))
    d = date_cls.fromisoformat(att_date)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(att_date: str, shift_start: str, shift_end: str) -> datetime:
    """Calculate shift end datetime, rolling over to next day for overnight shifts."""
    h, m = (int(x) for x in shift_end.split(":"))
    d = date_cls.fromisoformat(att_date)
    dt = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    if shift_end <= shift_start:  # overnight shift rollover
        dt += timedelta(days=1)
    return dt


def attendance_date_of(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """
    Determine the attendance date in IST for a punch-in timestamp.
    For overnight shifts, early morning punches before shift_end map back to previous day.
    """
    local = punch_in.astimezone(IST)
    d = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        d -= timedelta(days=1)
    return d.isoformat()


# ---- Derived Attendance Calculations -------------------------------------

def calc_late_minutes(punch_in: datetime, att_date: str, shift_start: str) -> int:
    """Calculate late arrival minutes beyond the 10-minute grace window."""
    start = shift_start_dt(att_date, shift_start)
    secs = (punch_in - start).total_seconds()
    if secs > 600:  # 10 min grace period (e.g. 09:40:00 is on time, 09:40:01 counts as 10 min late)
        return int(secs // 60)
    return 0


def calc_overtime_minutes(punch_out, att_date, shift_start, shift_end) -> int:
    """Calculate overtime minutes post shift-end, requiring a minimum 30-minute threshold."""
    end = shift_end_dt(att_date, shift_start, shift_end)
    mins = int((punch_out - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def calc_work_hours(punch_in, punch_out) -> float:
    """Calculate total working hours rounded to 2 decimal places."""
    secs = int((punch_out - punch_in).total_seconds())
    return float(
        (Decimal(secs) / Decimal(3600)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


# ---------------------------------------------------------------------------
# Serializers (formatting MongoDB docs into API responses without internal _id)
# ---------------------------------------------------------------------------
def _value_to_api(v):
    if isinstance(v, datetime):
        return to_millis(v)
    return v


def history_to_api(entry: dict) -> dict:
    changes = {}
    for key, chg in (entry.get("changes") or {}).items():
        changes[key] = {
            "from": _value_to_api(chg.get("from")),
            "to": _value_to_api(chg.get("to")),
        }
    return {
        "at": to_millis(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def attendance_to_api(doc: dict) -> dict:
    pi = doc.get("punch_in")
    po = doc.get("punch_out")
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_millis(pi) if pi else None,
        "punch_out": to_millis(po) if po else None,
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [history_to_api(h) for h in (doc.get("history") or [])],
    }


def employee_to_api(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_millis(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Database Indexes (created idempotently at application startup)
# ---------------------------------------------------------------------------
def ensure_indexes():
    # Enforce unique employee codes
    emp_col.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    # Optimize department summaries and trends
    emp_col.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_dept_joined"
    )
    emp_col.create_index([("joined_on", ASCENDING)], name="ix_joined_on")

    # One attendance log per employee per day; prevents concurrent duplicate punch-ins
    log_col.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date"
    )
    # Default sorting for attendance listing: date desc, emp_code asc
    log_col.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_date_emp"
    )
    # Filter for late arrivals within date ranges
    log_col.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING)], name="ix_date_late"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Pydantic Schemas & Validation
# ---------------------------------------------------------------------------
# Enforce strict integer epoch milliseconds (reject strings/floats)
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]

StatusLiteral = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceLiteral = Literal["PRESENT", "WFH", "ON_DUTY"]

HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=HHMM)
    shift_end: str = Field(default="18:30", pattern=HHMM)
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v):
        date_cls.fromisoformat(v)  # raises ValueError if invalid ISO format
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: PresenceLiteral = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    status: Optional[StatusLiteral] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Aggregation Pipeline Builders
# ---------------------------------------------------------------------------
def _presence(field: str = "$status"):
    return {"$in": [field, list(PRESENCE_STATUSES)]}


def _weekday(field: str = "$date"):
    """Match Monday through Friday; gracefully handles malformed date strings."""
    return {
        "$in": [
            {
                "$dayOfWeek": {
                    "$dateFromString": {"dateString": field, "onError": None}
                }
            },
            [2, 3, 4, 5, 6],  # MongoDB $dayOfWeek: 1=Sun, 2=Mon ... 6=Fri, 7=Sat
        ]
    }


def _half_day_weight():
    return {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}


def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    flt = {}
    if emp_code:
        flt["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        flt["date"] = rng
    if status:
        flt["status"] = status
    return flt


def build_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    """Build aggregation pipeline for employee monthly summary metrics."""
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [_presence(), _weekday()]},
                            _half_day_weight(),
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            {"$ifNull": ["$late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]


def build_department_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """
    Department metrics aggregated from attendance logs using the date index,
    then joining employees via $lookup to attach department info.
    """
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": [_presence(), _weekday()]},
                            _half_day_weight(),
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            {"$ifNull": ["$late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "on_duty_count": {
                    "$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}
                },
                # Track sums and counts across records to compute an exact true average later
                "wh_sum": {
                    "$sum": {"$cond": [_presence(), {"$ifNull": ["$work_hours", 0]}, 0]}
                },
                "wh_cnt": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    _presence(),
                                    {
                                        "$in": [
                                            {"$type": "$work_hours"},
                                            ["double", "int", "long", "decimal"],
                                        ]
                                    },
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        # Resolve department from the employees collection
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.append(
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {"$sum": "$present_days"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
                "wh_sum": {"$sum": "$wh_sum"},
                "wh_cnt": {"$sum": "$wh_cnt"},
            }
        }
    )
    return pipeline


def build_leaderboard_pipeline(
    first: str, last: str, department: Optional[str], limit: int
) -> list:
    """
    Rank employees with highest late minutes using standard competition ranking ($rank).
    Applies the limit after calculating ranks.
    """
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}, "late_minutes": {"$gt": 0}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        # Drop records where matching employee profile does not exist
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline += [
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},  # ties share the same rank, skipping subsequent numbers
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def build_trend_pipeline(department: str, from_s: str, to_s: str,
                         from_dt: datetime, to_dt: datetime) -> list:
    """
    Aggregate daily attendance trends and use $densify to fill gap dates across the range.
    """
    return [
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {
            "$group": {
                "_id": "$emp_code",
                "rows": {
                    "$push": {
                        "d": "$date",
                        "p": {"$cond": [_presence(), _half_day_weight(), 0]},
                        "l": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        },
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$unwind": "$rows"},
        {
            "$group": {
                "_id": "$rows.d",
                "present_count": {"$sum": "$rows.p"},
                "late_count": {"$sum": "$rows.l"},
            }
        },
        {"$project": {"_id": 0, "date": "$_id",
                      "present_count": 1, "late_count": 1}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$date"}}}},
        # Fill calendar date gaps directly in MongoDB
        {
            "$densify": {
                "field": "d",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        from_dt - timedelta(days=1),
                        to_dt + timedelta(days=1),
                    ],
                },
            }
        },
        {"$addFields": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}}},
        {"$match": {"date": {"$gte": from_s, "$lte": to_s}}},
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
    ]


# ---------------------------------------------------------------------------
# System Routes
# ---------------------------------------------------------------------------
@app.get("/health", tags=["system"])
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(status_code=503, detail="mongo not reachable")
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Employee Routes
# ---------------------------------------------------------------------------
@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": now_utc(),
    }
    try:
        emp_col.insert_one(doc)
    except DuplicateKeyError:
        # Handled gracefully if another insert happened concurrently
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return employee_to_api(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    flt = {"department": department} if department else {}
    total = emp_col.count_documents(flt)
    cursor = (
        emp_col.find(flt, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [employee_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# Attendance Routes (punch-in, punch-out, history & regularization)
# ---------------------------------------------------------------------------
@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    emp = emp_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        truncate_seconds(from_millis(body.punched_at))
        if body.punched_at is not None
        else now_utc()
    )
    att_date = attendance_date_of(punched_at, emp["shift_start"], emp["shift_end"])
    late = calc_late_minutes(punched_at, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        log_col.insert_one(doc)
    except DuplicateKeyError:
        # Guard against multiple punch-ins on the same date
        raise HTTPException(
            status_code=409, detail="already punched in for that date"
        )
    return attendance_to_api(doc)


@app.post("/attendance/punch-out", tags=["attendance"])
def punch_out(body: PunchOutRequest):
    emp = emp_col.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        truncate_seconds(from_millis(body.punched_at))
        if body.punched_at is not None
        else now_utc()
    )

    # Fetch latest open attendance record where punch_in is before punched_at
    rec = log_col.find_one(
        {
            "emp_code": body.emp_code,
            "punch_out": None,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(status_code=404, detail="no open punch-in found")

    pi = rec["punch_in"]
    if punched_at <= pi or (punched_at - pi) > timedelta(hours=24):
        raise HTTPException(
            status_code=422, detail="punched_at must be after punch_in and within 24h"
        )

    wh = calc_work_hours(pi, punched_at)
    ot = calc_overtime_minutes(
        punched_at, rec["date"], emp["shift_start"], emp["shift_end"]
    )
    hd = wh < 4.50  # half day if work hours under 4.5h

    # Atomic update: only updates if punch_out is still None to prevent double punch-outs
    updated = log_col.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": wh,
                "overtime_minutes": ot,
                "half_day": hd,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(status_code=409, detail="record already punched out")
    return attendance_to_api(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[date_cls] = Query(None),
    date_to: Optional[date_cls] = Query(None),
    status: Optional[StatusLiteral] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from > date_to")

    flt = build_attendance_filter(
        emp_code,
        date_from.isoformat() if date_from else None,
        date_to.isoformat() if date_to else None,
        status,
    )
    total = log_col.count_documents(flt)
    cursor = (
        log_col.find(flt)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [attendance_to_api(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize(emp_code: str, date: date_cls, body: RegularizeRequest):
    emp = emp_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    day = date.isoformat()
    rec = log_col.find_one({"emp_code": emp_code, "date": day})
    if not rec:
        raise HTTPException(status_code=404, detail="no record for that date")

    shift_start, shift_end = emp["shift_start"], emp["shift_end"]

    # Resolve target values (omitted fields remain unchanged)
    new_status = body.status if body.status is not None else rec["status"]
    new_in = (
        truncate_seconds(from_millis(body.punch_in))
        if body.punch_in is not None
        else rec.get("punch_in")
    )
    new_out = (
        truncate_seconds(from_millis(body.punch_out))
        if body.punch_out is not None
        else rec.get("punch_out")
    )

    # ABSENT / LEAVE cannot have punch timestamps
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422, detail="ABSENT/LEAVE cannot have punch times"
            )
        new_in = None
        new_out = None

    if new_status in PRESENCE_STATUSES and new_in is None:
        raise HTTPException(status_code=422, detail="presence status needs a punch_in")

    if new_in is not None:
        # Ensure new punch_in date matches the record attendance date
        if attendance_date_of(new_in, shift_start, shift_end) != day:
            raise HTTPException(
                status_code=422, detail="punch_in is not on the record's date"
            )

    if new_in is not None and new_out is not None:
        if new_out <= new_in or (new_out - new_in) > timedelta(hours=24):
            raise HTTPException(
                status_code=422, detail="punch_out must be after punch_in, within 24h"
            )

    # Recompute derived metrics based on updated timestamps
    if new_status in PRESENCE_STATUSES and new_in is not None:
        new_late = calc_late_minutes(new_in, day, shift_start)
    else:
        new_late = 0

    if new_in is not None and new_out is not None:
        new_wh = calc_work_hours(new_in, new_out)
        new_ot = calc_overtime_minutes(new_out, day, shift_start, shift_end)
        new_hd = new_wh < 4.50
    else:
        new_wh, new_ot, new_hd = None, 0, False

    # Record only the fields that actually changed
    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    new = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": new_wh,
        "late_minutes": new_late,
        "overtime_minutes": new_ot,
        "half_day": new_hd,
    }
    changes = {}
    for key in old:
        if old[key] != new[key]:
            changes[key] = {"from": old[key], "to": new[key]}

    if not changes:
        raise HTTPException(status_code=422, detail="nothing changed")

    entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: guard with history state to avoid overwriting concurrent edits
    flt = {"_id": rec["_id"]}
    if "history" in rec:
        flt["history"] = rec["history"]
    else:
        flt["history"] = {"$exists": False}

    res = log_col.update_one(
        flt, {"$set": new, "$push": {"history": entry}}
    )
    if res.matched_count == 0:
        # Failed CAS update indicates a concurrent modification
        raise HTTPException(status_code=409, detail="record modified concurrently")

    fresh = log_col.find_one({"_id": rec["_id"]})
    return attendance_to_api(fresh)


# ---------------------------------------------------------------------------
# Analytics Routes
# ---------------------------------------------------------------------------
MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)
):
    emp = emp_col.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="employee not found")

    first_d, last_d = month_bounds(month)
    first, last = first_d.isoformat(), last_d.isoformat()

    rows = list(log_col.aggregate(build_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}

    # For employees joining mid-month, count working days from their joined_on date
    start = max(first_d, date_cls.fromisoformat(emp["joined_on"]))
    working_days = count_working_days(start, last_d)

    present_days = float(agg.get("present_days", 0) or 0)
    pct = None
    if working_days > 0:
        pct = round_half_up(present_days / working_days * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0) or 0),
        "late_count": int(agg.get("late_count", 0) or 0),
        "total_late_minutes": int(agg.get("total_late_minutes", 0) or 0),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0) or 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None),
):
    first_d, last_d = month_bounds(month)
    first, last = first_d.isoformat(), last_d.isoformat()

    metrics = {
        r["_id"]: r
        for r in log_col.aggregate(build_department_pipeline(first, last, department))
    }

    # Consider headcount of employees joined on or before month end
    hc_match = {"joined_on": {"$lte": last}}
    if department:
        hc_match["department"] = department
    headcounts = list(
        emp_col.aggregate(
            [
                {"$match": hc_match},
                {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
            ]
        )
    )

    items = []
    for row in headcounts:
        dept = row["_id"]
        m = metrics.get(dept, {})
        wh_sum = m.get("wh_sum", 0) or 0
        wh_cnt = m.get("wh_cnt", 0) or 0
        items.append(
            {
                "department": dept,
                "headcount": int(row["headcount"]),
                "present_days": round_half_up(m.get("present_days", 0) or 0, 2),
                "avg_work_hours": (
                    round_half_up(wh_sum / wh_cnt, 2) if wh_cnt else None
                ),
                "late_count": int(m.get("late_count", 0) or 0),
                "total_late_minutes": int(m.get("total_late_minutes", 0) or 0),
                "leave_count": int(m.get("leave_count", 0) or 0),
                "on_duty_count": int(m.get("on_duty_count", 0) or 0),
            }
        )
    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    first_d, last_d = month_bounds(month)
    first, last = first_d.isoformat(), last_d.isoformat()

    rows = list(
        log_col.aggregate(
            build_leaderboard_pipeline(first, last, department, limit)
        )
    )
    return {"month": month, "items": rows}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str,
    from_: date_cls = Query(..., alias="from"),
    to: date_cls = Query(...),
):
    if to < from_:
        raise HTTPException(status_code=422, detail="to < from")
    if (to - from_).days + 1 > 92:
        raise HTTPException(status_code=422, detail="range exceeds 92 days")

    if emp_col.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="unknown department")

    from_s, to_s = from_.isoformat(), to.isoformat()
    from_dt = datetime(from_.year, from_.month, from_.day, tzinfo=timezone.utc)
    to_dt = datetime(to.year, to.month, to.day, tzinfo=timezone.utc)

    rows = list(
        log_col.aggregate(
            build_trend_pipeline(department, from_s, to_s, from_dt, to_dt)
        )
    )
    have = {r["date"]: r for r in rows}

    # Calculate cumulative department headcount per day
    hc_rows = list(
        emp_col.aggregate(
            [
                {"$match": {"department": department}},
                {"$group": {"_id": "$joined_on", "n": {"$sum": 1}}},
            ]
        )
    )
    joined = sorted((r["_id"], r["n"]) for r in hc_rows)

    items, rates = [], []
    total_days = (to - from_).days + 1
    for i in range(total_days):
        d = from_ + timedelta(days=i)
        ds = d.isoformat()
        base = have.get(ds) or {"present_count": 0, "late_count": 0}
        hc = sum(n for j, n in joined if j <= ds)
        is_wd = d.weekday() < 5
        rate = (
            round_half_up(base["present_count"] / hc, 4)
            if (is_wd and hc > 0)
            else None
        )
        rates.append(rate)
        items.append(
            {
                "date": ds,
                "is_working_day": is_wd,
                "headcount": hc,
                "present_count": float(base["present_count"]),
                "late_count": int(base["late_count"]),
                "attendance_rate": rate,
                "moving_avg_7d": None,
            }
        )

    # 7-day moving average: mean of available non-null rates over the trailing 7 days
    for i, it in enumerate(items):
        window = [r for r in rates[max(0, i - 6): i + 1] if r is not None]
        it["moving_avg_7d"] = (
            round_half_up(sum(window) / len(window), 4) if window else None
        )

    return {"department": department, "items": items}


# ---------------------------------------------------------------------------
# Admin & Diagnostic Routes
# ---------------------------------------------------------------------------
def _jsonable(obj):
    """Convert MongoDB explain command result objects into JSON-safe formats."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, datetime):
        return to_millis(obj)
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ] = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date_cls] = Query(None),
    date_to: Optional[date_cls] = Query(None),
    status: Optional[StatusLiteral] = Query(None),
    from_: Optional[date_cls] = Query(None, alias="from"),
    to: Optional[date_cls] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(cond, name):
        if not cond:
            raise HTTPException(status_code=422, detail=f"missing parameter: {name}")

    if endpoint == "attendance_list":
        flt = build_attendance_filter(
            emp_code,
            date_from.isoformat() if date_from else None,
            date_to.isoformat() if date_to else None,
            status,
        )
        cmd = {
            "find": "attendance_logs",
            "filter": flt,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        result = db.command("explain", cmd, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _jsonable(result)}

    if endpoint == "employee_monthly":
        need(emp_code, "emp_code")
        need(month, "month")
        first_d, last_d = month_bounds(month)
        pipeline = build_monthly_pipeline(
            emp_code, first_d.isoformat(), last_d.isoformat()
        )
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _jsonable(result)}

    if endpoint == "department_summary":
        need(month, "month")
        first_d, last_d = month_bounds(month)
        pipeline = build_department_pipeline(
            first_d.isoformat(), last_d.isoformat(), department
        )
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _jsonable(result)}

    if endpoint == "late_leaderboard":
        need(month, "month")
        first_d, last_d = month_bounds(month)
        pipeline = build_leaderboard_pipeline(
            first_d.isoformat(), last_d.isoformat(), department, limit
        )
        result = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _jsonable(result)}

    # department_trend
    need(department, "department")
    need(from_, "from")
    need(to, "to")
    from_dt = datetime(from_.year, from_.month, from_.day, tzinfo=timezone.utc)
    to_dt = datetime(to.year, to.month, to.day, tzinfo=timezone.utc)
    pipeline = build_trend_pipeline(
        department, from_.isoformat(), to.isoformat(), from_dt, to_dt
    )
    result = db.command(
        "explain",
        {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )
    return {"endpoint": endpoint, "collection": "attendance_logs",
            "explain": _jsonable(result)}