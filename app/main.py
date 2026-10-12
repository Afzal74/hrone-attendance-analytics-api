"""Employee Attendance & Analytics API."""

import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

# --------------------------------------------------------------------------- #
# Configuration, timezone, and status constants
# --------------------------------------------------------------------------- #
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")

# --------------------------------------------------------------------------- #
# MongoDB client and database
# --------------------------------------------------------------------------- #
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# --------------------------------------------------------------------------- #
# Validation and time/business-rule helpers
# --------------------------------------------------------------------------- #
def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid YYYY-MM-DD date") from exc


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("must be a valid YYYY-MM month")
    year, month = map(int, value.split("-"))
    first = date(year, month, 1)
    return first, date(year, month, calendar.monthrange(year, month)[1])


def epoch_to_datetime(value: int) -> datetime:
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("must be an integer epoch timestamp in milliseconds")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)


def datetime_to_epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.astimezone(UTC).timestamp() * 1000)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def ist_date(value: datetime) -> date:
    return normalize_datetime(value).astimezone(IST).date()


def shift_datetime(day: date, shift: str, overnight_day: bool = False) -> datetime:
    hour, minute = map(int, shift.split(":"))
    result = datetime(day.year, day.month, day.day, hour, minute, tzinfo=IST)
    if overnight_day:
        result += timedelta(days=1)
    return result.astimezone(UTC).replace(microsecond=0)


def attendance_date(punch_in: datetime, employee: dict[str, Any]) -> str:
    local = normalize_datetime(punch_in).astimezone(IST)
    start = employee["shift_start"]
    end = employee["shift_end"]
    if end <= start and local.strftime("%H:%M") < end:
        return (local.date() - timedelta(days=1)).isoformat()
    return local.date().isoformat()


def shift_boundaries(punch_in: datetime, employee: dict[str, Any]) -> tuple[datetime, datetime]:
    local = normalize_datetime(punch_in).astimezone(IST)
    start_text = employee["shift_start"]
    end_text = employee["shift_end"]
    day = local.date()
    overnight = end_text <= start_text
    if overnight and local.strftime("%H:%M") < end_text:
        day -= timedelta(days=1)
    return (
        shift_datetime(day, start_text),
        shift_datetime(day, end_text, overnight),
    )


def compute_late_minutes(punch_in: datetime, employee: dict[str, Any]) -> int:
    start, _ = shift_boundaries(punch_in, employee)
    elapsed_seconds = (normalize_datetime(punch_in) - start).total_seconds()
    elapsed_minutes = int(elapsed_seconds // 60)
    return elapsed_minutes if elapsed_seconds > 10 * 60 else 0


def decimal_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((normalize_datetime(punch_out) - normalize_datetime(punch_in)).total_seconds()))
    hours = (seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_derived(
    punch_in: Optional[datetime],
    punch_out: Optional[datetime],
    employee: dict[str, Any],
) -> dict[str, Any]:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    punch_in = normalize_datetime(punch_in)
    late = compute_late_minutes(punch_in, employee)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    punch_out = normalize_datetime(punch_out)
    work_hours = decimal_hours(punch_in, punch_out)
    _, shift_end = shift_boundaries(punch_in, employee)
    overtime_elapsed = int((punch_out - shift_end).total_seconds() // 60)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": overtime_elapsed if overtime_elapsed >= 30 else 0,
        "half_day": work_hours < 4.50,
    }


# --------------------------------------------------------------------------- #
# API serialization and pagination helpers
# --------------------------------------------------------------------------- #
def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return datetime_to_epoch(value)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items() if key != "_id"}
    return value


def public_doc(doc: dict[str, Any]) -> dict[str, Any]:
    result = dict(doc)
    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)
    return json_value(result)


def page_params(page: int, page_size: int) -> None:
    if page < 1 or not 1 <= page_size <= 100:
        raise HTTPException(422, "page must be >= 1 and page_size must be between 1 and 100")


def validate_range(date_from: Optional[str], date_to: Optional[str]) -> None:
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


# --------------------------------------------------------------------------- #
# Request validation models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value: str) -> str:
        parse_date(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "emp_code": "EMP0001",
                "punched_at": 1783312500000,
                "status": "PRESENT",
            }
        },
    )

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            epoch_to_datetime(value)
        return value

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: str) -> str:
        if value not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return value


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "emp_code": "EMP0001",
                "punched_at": 1783344900000,
            }
        },
    )

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            epoch_to_datetime(value)
        return value


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "status": "WFH",
                "reason": "Approved work from home",
                "regularized_by": "HR Admin",
            }
        },
    )

    @field_validator("status")
    @classmethod
    def valid_status(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in STATUSES:
            raise ValueError("invalid status")
        return value

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value: Optional[int]) -> Optional[int]:
        if value is not None:
            epoch_to_datetime(value)
        return value


# --------------------------------------------------------------------------- #
# MongoDB indexes and application lifecycle
# --------------------------------------------------------------------------- #
def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)],
        name="attendance_emp_date_status",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_out", ASCENDING), ("punch_in", DESCENDING)],
        name="attendance_open_punch",
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Health and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict[str, str]:
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn) -> dict[str, Any]:
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    return public_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    docs = db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [public_doc(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance punch-in and punch-out
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record_date = attendance_date(punched_at, employee)
    derived = compute_derived(punched_at, None, employee)
    doc = {
        "emp_code": body.emp_code,
        "date": record_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return public_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn) -> dict[str, Any]:
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    punched_at = epoch_to_datetime(body.punched_at) if body.punched_at is not None else datetime.now(UTC).replace(microsecond=0)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_value = normalize_datetime(record["punch_in"])
    if punched_at <= punch_in_value or punched_at > punch_in_value + timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = compute_derived(punch_in_value, punched_at, employee)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record is already punched out")
    return public_doc(updated)


# --------------------------------------------------------------------------- #
# Attendance listing, filtering, and pagination
# --------------------------------------------------------------------------- #
def attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    status: Optional[str],
) -> dict[str, Any]:
    validate_range(date_from, date_to)
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    query: dict[str, Any] = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    return query


def attendance_list_sort() -> dict[str, int]:
    return {"date": DESCENDING, "emp_code": ASCENDING}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    query = attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    cursor = (
        db.attendance_logs.find(query)
        .sort(list(attendance_list_sort().items()))
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [public_doc(doc) for doc in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Analytics helpers and MongoDB aggregation pipelines
# --------------------------------------------------------------------------- #
def working_days(first: date, last: date) -> int:
    if last < first:
        return 0
    return sum((first + timedelta(days=i)).weekday() < 5 for i in range((last - first).days + 1))


def month_query(month: str) -> tuple[date, date]:
    try:
        return parse_month(month)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def present_value(doc: dict[str, Any]) -> float:
    if doc.get("status") not in PRESENCE:
        return 0.0
    return 0.5 if doc.get("half_day", False) else 1.0


def round_half_up(value: Optional[float], places: str) -> Optional[float]:
    if value is None:
        return None
    return float(Decimal(str(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list[dict[str, Any]]:
    first_dt = datetime(first.year, first.month, first.day)
    day_count = (last - first).days + 1
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {
            "joinedDate": {"$dateFromString": {"dateString": "$joined_on"}},
            "calendarDays": {
                "$map": {
                    "input": {"$range": [0, day_count]},
                    "as": "offset",
                    "in": {"$dateAdd": {"startDate": first_dt, "unit": "day", "amount": "$$offset"}},
                }
            },
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", first.isoformat()]},
                    {"$lte": ["$date", last.isoformat()]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$project": {
                    "_id": 0,
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "_id": 0,
            "emp_code": 1,
            "working_days": {"$size": {"$filter": {
                "input": "$calendarDays",
                "as": "day",
                "cond": {"$and": [
                    {"$gte": ["$$day", "$joinedDate"]},
                    {"$in": [{"$dayOfWeek": "$$day"}, [2, 3, 4, 5, 6]]},
                ]},
            }}},
            "present_days": {"$sum": {"$map": {
                "input": "$logs",
                "as": "log",
                "in": {"$cond": [
                    {"$and": [
                        {"$gte": [{"$dateFromString": {"dateString": "$$log.date"}}, "$joinedDate"]},
                        {"$in": ["$$log.status", list(PRESENCE)]},
                        {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$log.date"}}}, [2, 3, 4, 5, 6]]},
                    ]},
                    {"$cond": ["$$log.half_day", 0.5, 1]},
                    0,
                ]},
            }}},
            "leave_days": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
                {"$eq": ["$$log.status", "LEAVE"]},
                {"$gte": ["$$log.date", {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedDate"}}]},
            ]}}}},
            "late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
                {"$gt": ["$$log.late_minutes", 0]},
                {"$gte": ["$$log.date", {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedDate"}}]},
            ]}}}},
            "total_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                {"$gte": ["$$log.date", {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedDate"}}]},
                "$$log.late_minutes",
                0,
            ]}}}},
            "total_overtime_minutes": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$cond": [
                {"$gte": ["$$log.date", {"$dateToString": {"format": "%Y-%m-%d", "date": "$joinedDate"}}]},
                "$$log.overtime_minutes",
                0,
            ]}}}},
        }},
    ]


# --------------------------------------------------------------------------- #
# Attendance regularization and audit history
# --------------------------------------------------------------------------- #
@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn) -> dict[str, Any]:
    try:
        parse_date(date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    employee = db.employees.find_one({"emp_code": emp_code})
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if employee is None or record is None:
        raise HTTPException(404, "employee or attendance record not found")
    old_status = record.get("status")
    new_status = body.status if body.status is not None else old_status
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")
    new_in = epoch_to_datetime(body.punch_in) if body.punch_in is not None else old_in
    new_out = epoch_to_datetime(body.punch_out) if body.punch_out is not None else old_out
    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT and LEAVE records cannot have punch times")
        new_in = new_out = None
    elif new_in is None:
        raise HTTPException(422, "a presence status requires punch_in")
    if new_in is not None:
        new_in = normalize_datetime(new_in)
        if attendance_date(new_in, employee) != date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
    if new_out is not None:
        new_out = normalize_datetime(new_out)
    if new_in is not None and new_out is not None:
        if new_out <= new_in or new_out > new_in + timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")
    elif new_out is not None:
        raise HTTPException(422, "punch_out requires punch_in")
    derived = compute_derived(new_in, new_out, employee)
    final = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in tracked:
        before = record.get(field)
        after = final.get(field)
        if isinstance(before, datetime):
            before = normalize_datetime(before)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")
    entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "history": record.get("history", [])},
        {"$set": final, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was changed concurrently")
    return public_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics: employee monthly summary
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str) -> dict[str, Any]:
    first, last = month_query(month)
    if db.employees.count_documents({"emp_code": emp_code}, limit=1) == 0:
        raise HTTPException(404, "employee not found")
    result = next(iter(db.employees.aggregate(employee_monthly_pipeline(emp_code, first, last))), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    days = result["working_days"]
    present = result["present_days"]
    pct = None if days == 0 else round_half_up(present / days * 100, "0.01")
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": days,
        "present_days": round_half_up(present, "0.01"),
        "leave_days": result["leave_days"],
        "late_count": result["late_count"],
        "total_late_minutes": result["total_late_minutes"],
        "total_overtime_minutes": result["total_overtime_minutes"],
        "attendance_pct": pct,
    }


# --------------------------------------------------------------------------- #
# Analytics: department summary and headcount
# --------------------------------------------------------------------------- #
def department_summary_pipeline(first: date, last: date, department: Optional[str]) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {
            "joined_on": {"$lte": last.isoformat()},
            **({"department": department} if department is not None else {}),
        }},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code", "joined": "$joined_on"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", first.isoformat()]},
                    {"$lte": ["$date", last.isoformat()]},
                    {"$gte": ["$date", "$$joined"]},
                ]}}},
                {"$project": {
                    "_id": 0,
                    "date": 1,
                    "status": 1,
                    "half_day": {"$ifNull": ["$half_day", False]},
                    "work_hours": 1,
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code",
            "department": {"$first": "$department"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "1900-01-01"]}}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                0,
            ]}},
            "hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                "$logs.work_hours",
                0,
            ]}},
            "hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", list(PRESENCE)]},
                    {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]},
                ]},
                1,
                0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": "$logs.late_minutes"},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": "$present_days"},
            "hours_sum": {"$sum": "$hours_sum"},
            "hours_count": {"$sum": "$hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": 1,
            "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$hours_count", 0]},
                {"$divide": ["$hours_sum", "$hours_count"]},
                None,
            ]},
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None) -> dict[str, Any]:
    first, last = month_query(month)
    items = list(db.employees.aggregate(department_summary_pipeline(first, last, department)))
    for item in items:
        item["present_days"] = round_half_up(item["present_days"], "0.01")
        item["avg_work_hours"] = round_half_up(item["avg_work_hours"], "0.01")
    return {"month": month, "items": items}


# --------------------------------------------------------------------------- #
# Analytics: late leaderboard and tie ranking
# --------------------------------------------------------------------------- #
def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str]) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}},
        {"$setWindowFields": {
            "partitionBy": None,
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}},
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None) -> dict[str, Any]:
    first, last = month_query(month)
    rows = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": rows}


# --------------------------------------------------------------------------- #
# Analytics: department trend and gap filling
# --------------------------------------------------------------------------- #
def trend_date_range(from_date: str, to: str) -> tuple[date, date]:
    try:
        first, last = parse_date(from_date), parse_date(to)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if last < first:
        raise HTTPException(422, "to must not be before from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "trend range cannot exceed 92 days")
    return first, last


def department_trend_pipeline(department: str, first: date, last: date) -> list[dict[str, Any]]:
    from_date = first.isoformat()
    to = last.isoformat()
    first_dt = datetime(first.year, first.month, first.day)
    end_dt = datetime(last.year, last.month, last.day)
    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": from_date, "$lte": to}}},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "employee",
        }},
        {"$unwind": "$employee"},
        {"$match": {"employee.department": department}},
        {"$match": {"$expr": {"$gte": [
            {"$dateFromString": {"dateString": "$date"}},
            {"$dateFromString": {"dateString": "$employee.joined_on"}},
        ]}}},
        {"$project": {
            "day": {"$dateFromString": {"dateString": "$date"}},
            "present_count": {"$cond": [
                {"$in": ["$status", list(PRESENCE)]},
                {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                0,
            ]},
            "late_count": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]},
        }},
        {"$unionWith": {
            "coll": "attendance_logs",
            "pipeline": [{"$documents": [
                {"day": first_dt, "present_count": 0, "late_count": 0},
                {"day": end_dt, "present_count": 0, "late_count": 0},
            ]}],
        }},
        {"$group": {
            "_id": "$day",
            "present_count": {"$sum": "$present_count"},
            "late_count": {"$sum": "$late_count"},
        }},
        {"$project": {"_id": 0, "day": "$_id", "present_count": 1, "late_count": 1}},
        {"$sort": {"day": 1}},
        {"$densify": {
            "field": "day",
            "range": {"step": 1, "unit": "day", "bounds": [first_dt, end_dt + timedelta(days=1)]},
        }},
        {"$set": {
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$lookup": {
            "from": "employees",
            "pipeline": [
                {"$match": {"department": department}},
                {"$project": {"_id": 0, "joinedDate": {"$dateFromString": {"dateString": "$joined_on"}}}},
            ],
            "as": "departmentEmployees",
        }},
        {"$set": {
            "headcount": {"$size": {"$filter": {
                "input": "$departmentEmployees",
                "as": "employee",
                "cond": {"$lte": ["$$employee.joinedDate", "$day"]},
            }}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {
            "attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]},
        }},
        {"$setWindowFields": {
            "sortBy": {"day": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                },
            },
        }},
        {"$match": {"day": {"$gte": first_dt, "$lt": end_dt + timedelta(days=1)}}},
        {"$project": {
            "_id": 0,
            "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": 1,
            "headcount": 1,
            "present_count": 1,
            "late_count": 1,
            "attendance_rate": 1,
            "moving_avg_7d": 1,
        }},
        {"$sort": {"date": 1}},
    ]
    return pipeline


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)) -> dict[str, Any]:
    first, last = trend_date_range(from_date, to)
    if db.employees.count_documents({"department": department}, limit=1) == 0:
        raise HTTPException(404, "department not found")
    items = list(db.attendance_logs.aggregate(department_trend_pipeline(department, first, last)))
    for item in items:
        item["attendance_rate"] = round_half_up(item["attendance_rate"], "0.0001")
        item["moving_avg_7d"] = round_half_up(item["moving_avg_7d"], "0.0001")
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# MongoDB explain endpoint and index verification
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    page_params(page, page_size)
    if endpoint == "attendance_list":
        query = attendance_list_query(emp_code, date_from, date_to, status)
        explain = db.command("explain", {"find": "attendance_logs", "filter": query, "sort": attendance_list_sort(), "skip": (page - 1) * page_size, "limit": page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if month is None:
            raise HTTPException(422, "month is required")
        first, last = month_query(month)
        if endpoint == "employee_monthly":
            if not emp_code:
                raise HTTPException(422, "emp_code is required")
            collection = "employees"
            pipeline = employee_monthly_pipeline(emp_code, first, last)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(first, last, limit, department)
        else:
            collection = "employees"
            pipeline = department_summary_pipeline(first, last, department)
        explain = db.command("explain", {"aggregate": collection, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection": collection, "explain": explain}
    if endpoint == "department_trend":
        if department is None or from_date is None or to is None:
            raise HTTPException(422, "department, from, and to are required")
        first, last = trend_date_range(from_date, to)
        if db.employees.count_documents({"department": department}, limit=1) == 0:
            raise HTTPException(404, "department not found")
        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": department_trend_pipeline(department, first, last),
                "cursor": {},
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
    raise HTTPException(422, "unknown endpoint")
