import unittest
from datetime import datetime, timezone

from fastapi import HTTPException

from app import main


class FakeEmployees:
    def find_one(self, query):
        return {
            "emp_code": query["emp_code"],
            "shift_start": "09:30",
            "shift_end": "18:30",
        }


class FakeAttendanceLogs:
    def __init__(self, record=None):
        self.record = record

    def find_one(self, _query, **_kwargs):
        if self.record is None:
            return None
        return dict(self.record)

    def find_one_and_update(self, _query, update, **_kwargs):
        if self.record is None or self.record.get("punch_out") is not None:
            return None

        self.record.update(update["$set"])
        return dict(self.record)


class FakeDb:
    def __init__(self, record=None):
        self.employees = FakeEmployees()
        self.attendance_logs = FakeAttendanceLogs(record)


class AttendanceRulesTests(unittest.TestCase):

    # ---------------------------------------------------------
    # R2 - Late grace boundary
    # ---------------------------------------------------------
    def test_late_grace_boundary(self):
        employee = {
            "shift_start": "09:30",
            "shift_end": "18:30",
        }

        # 09:40:00 -> exactly 10 minutes -> NOT late
        self.assertEqual(
            main.compute_late_minutes(
                datetime(2025, 1, 2, 4, 10, tzinfo=timezone.utc),
                employee,
            ),
            0,
        )

        # 09:40:01 -> strictly more than 10 minutes -> 10 late minutes
        self.assertEqual(
            main.compute_late_minutes(
                datetime(2025, 1, 2, 4, 10, 1, tzinfo=timezone.utc),
                employee,
            ),
            10,
        )

        # 10:15:00 -> 45 minutes after shift start
        self.assertEqual(
            main.compute_late_minutes(
                datetime(2025, 1, 2, 4, 45, tzinfo=timezone.utc),
                employee,
            ),
            45,
        )

    # ---------------------------------------------------------
    # R3 - Overtime boundary
    # ---------------------------------------------------------
    def test_overtime_boundary(self):
        employee = {
            "shift_start": "09:30",
            "shift_end": "18:30",
        }

        punch_in = datetime(
            2025, 1, 2, 4, 0, tzinfo=timezone.utc
        )

        # Shift ends at 18:30 IST = 13:00 UTC

        # 29 minutes after shift end -> no overtime
        punch_out_29 = datetime(
            2025, 1, 2, 13, 29, tzinfo=timezone.utc
        )

        result_29 = main.compute_derived(
            punch_in,
            punch_out_29,
            employee,
        )

        self.assertEqual(result_29["overtime_minutes"], 0)

        # Exactly 30 minutes after shift end -> 30 overtime minutes
        punch_out_30 = datetime(
            2025, 1, 2, 13, 30, tzinfo=timezone.utc
        )

        result_30 = main.compute_derived(
            punch_in,
            punch_out_30,
            employee,
        )

        self.assertEqual(result_30["overtime_minutes"], 30)

    # ---------------------------------------------------------
    # R4 - Work hours HALF_UP rounding
    # ---------------------------------------------------------
    def test_work_hours_half_up_rounding(self):
        punch_in = datetime(
            2025, 1, 2, 0, 0, tzinfo=timezone.utc
        )

        # 4.495 hours -> HALF_UP -> 4.50
        punch_out = datetime(
            2025, 1, 2, 4, 29, 42, tzinfo=timezone.utc
        )

        self.assertEqual(
            main.decimal_hours(punch_in, punch_out),
            4.50,
        )

        # 4.494 hours -> 4.49
        punch_out = datetime(
            2025, 1, 2, 4, 29, 38, 400000, tzinfo=timezone.utc
        )

        self.assertEqual(
            main.decimal_hours(punch_in, punch_out),
            4.49,
        )

    # ---------------------------------------------------------
    # R5 - Half-day boundary
    # ---------------------------------------------------------
    def test_half_day_boundary(self):
        employee = {
            "shift_start": "09:30",
            "shift_end": "18:30",
        }

        punch_in = datetime(
            2025, 1, 2, 4, 0, tzinfo=timezone.utc
        )

        # 4.49 hours -> half day
        punch_out_449 = datetime(
            2025, 1, 2, 8, 29, 24, tzinfo=timezone.utc
        )

        result_449 = main.compute_derived(
            punch_in,
            punch_out_449,
            employee,
        )

        self.assertEqual(result_449["work_hours"], 4.49)
        self.assertTrue(result_449["half_day"])

        # Exactly 4.50 hours -> NOT half day
        punch_out_450 = datetime(
            2025, 1, 2, 8, 30, tzinfo=timezone.utc
        )

        result_450 = main.compute_derived(
            punch_in,
            punch_out_450,
            employee,
        )

        self.assertEqual(result_450["work_hours"], 4.50)
        self.assertFalse(result_450["half_day"])

        # 4.495 rounds to 4.50 -> NOT half day
        punch_out_4495 = datetime(
            2025, 1, 2, 8, 29, 42, tzinfo=timezone.utc
        )

        result_4495 = main.compute_derived(
            punch_in,
            punch_out_4495,
            employee,
        )

        self.assertEqual(result_4495["work_hours"], 4.50)
        self.assertFalse(result_4495["half_day"])

    # ---------------------------------------------------------
    # Open punch-in record
    # ---------------------------------------------------------
    def test_open_record_has_no_work_hours(self):
        employee = {
            "shift_start": "09:30",
            "shift_end": "18:30",
        }

        punch_in = datetime(
            2025, 1, 2, 4, 0, tzinfo=timezone.utc
        )

        result = main.compute_derived(
            punch_in,
            None,
            employee,
        )

        self.assertIsNone(result["work_hours"])
        self.assertEqual(result["overtime_minutes"], 0)
        self.assertFalse(result["half_day"])

    # ---------------------------------------------------------
    # R6 - Presence statuses
    # ---------------------------------------------------------
    def test_presence_statuses(self):
        # These three count as present
        self.assertEqual(
            main.present_value({"status": "PRESENT"}),
            1.0,
        )

        self.assertEqual(
            main.present_value({"status": "WFH"}),
            1.0,
        )

        self.assertEqual(
            main.present_value({"status": "ON_DUTY"}),
            1.0,
        )

        # These two do NOT count as present
        self.assertEqual(
            main.present_value({"status": "ABSENT"}),
            0.0,
        )

        self.assertEqual(
            main.present_value({"status": "LEAVE"}),
            0.0,
        )

    # ---------------------------------------------------------
    # R6 - Half day counts as 0.5 present day
    # ---------------------------------------------------------
    def test_half_day_presence_value(self):
        self.assertEqual(
            main.present_value({
                "status": "PRESENT",
                "half_day": True,
            }),
            0.5,
        )

        self.assertEqual(
            main.present_value({
                "status": "WFH",
                "half_day": True,
            }),
            0.5,
        )

        self.assertEqual(
            main.present_value({
                "status": "ON_DUTY",
                "half_day": True,
            }),
            0.5,
        )

    # ---------------------------------------------------------
    # R7 - Working days exclude weekends
    # ---------------------------------------------------------
    def test_working_days_exclude_weekends(self):
        # Monday 2026-07-06 through Sunday 2026-07-12
        # Monday-Friday = 5 working days
        self.assertEqual(
            main.working_days(
                datetime(2026, 7, 6).date(),
                datetime(2026, 7, 12).date(),
            ),
            5,
        )

    # ---------------------------------------------------------
    # R7 - Weekend-only range
    # ---------------------------------------------------------
    def test_weekend_only_has_zero_working_days(self):
        # Saturday + Sunday
        self.assertEqual(
            main.working_days(
                datetime(2026, 7, 11).date(),
                datetime(2026, 7, 12).date(),
            ),
            0,
        )

    # ---------------------------------------------------------
    # R7 - Mid-month joined employee
    # ---------------------------------------------------------
    def test_working_days_from_join_date(self):
        # July 13, 2026 -> July 31, 2026
        # 15 weekdays
        self.assertEqual(
            main.working_days(
                datetime(2026, 7, 13).date(),
                datetime(2026, 7, 31).date(),
            ),
            15,
        )

    # ---------------------------------------------------------
    # Punch-out: no record -> 404
    # ---------------------------------------------------------
    def test_punch_out_no_record_is_404(self):
        original_db = main.db
        main.db = FakeDb()

        try:
            with self.assertRaisesRegex(
                HTTPException,
                "no punch-in found",
            ):
                main.punch_out(
                    main.PunchOutIn(
                        emp_code="EMP0001",
                        punched_at=1735790400000,
                    )
                )
        finally:
            main.db = original_db

    # ---------------------------------------------------------
    # Punch-out: already closed -> 409
    # ---------------------------------------------------------
    def test_punch_out_closed_record_is_409(self):
        record = {
            "_id": "record-1",
            "punch_in": datetime(
                2025, 1, 2, 4, 0, tzinfo=timezone.utc
            ),
            "punch_out": datetime(
                2025, 1, 2, 12, 0, tzinfo=timezone.utc
            ),
        }

        original_db = main.db
        main.db = FakeDb(record)

        try:
            with self.assertRaisesRegex(
                HTTPException,
                "already punched out",
            ):
                main.punch_out(
                    main.PunchOutIn(
                        emp_code="EMP0001",
                        punched_at=1735826400000,
                    )
                )
        finally:
            main.db = original_db

    # ---------------------------------------------------------
    # Punch-out: second attempt -> 409
    # ---------------------------------------------------------
    def test_only_one_punch_out_succeeds_for_same_record(self):
        record = {
            "_id": "record-1",
            "punch_in": datetime(
                2025, 1, 2, 4, 0, tzinfo=timezone.utc
            ),
            "punch_out": None,
        }

        original_db = main.db
        main.db = FakeDb(record)

        try:
            first = main.punch_out(
                main.PunchOutIn(
                    emp_code="EMP0001",
                    punched_at=1735826400000,
                )
            )

            self.assertIsNotNone(first["punch_out"])

            with self.assertRaisesRegex(
                HTTPException,
                "already punched out",
            ):
                main.punch_out(
                    main.PunchOutIn(
                        emp_code="EMP0001",
                        punched_at=1735826400000,
                    )
                )
        finally:
            main.db = original_db


if __name__ == "__main__":
    unittest.main()