"""Comprehensive test suite for all API endpoints in Employee Attendance & Analytics API."""
import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8000"


def req(method: str, path: str, data: dict = None, expected_status: int = 200):
    url = f"{BASE_URL}{path}"
    headers = {"Content-Type": "application/json"} if data else {}
    body = json.dumps(data).encode("utf-8") if data else None

    req_obj = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req_obj) as resp:
            code = resp.getcode()
            res_body = json.loads(resp.read().decode("utf-8"))
            assert code == expected_status, f"Expected {expected_status}, got {code}"
            return code, res_body
    except urllib.error.HTTPError as e:
        err_body = json.loads(e.read().decode("utf-8")) if e.fp else {}
        assert e.code == expected_status, f"Expected {expected_status}, got {e.code}: {err_body}"
        return e.code, err_body


passed = 0
failed = 0


def test(name, fn):
    global passed, failed
    try:
        fn()
        print(f" [PASS] {name}")
        passed += 1
    except Exception as e:
        print(f" [FAIL] {name}: {e}")
        failed += 1


# 1. Health
def test_health():
    code, data = req("GET", "/health", expected_status=200)
    assert data.get("status") == "ok", data


# 2. Employees API
def test_create_and_duplicate_employee():
    # Clean up test employee if exists
    test_emp = {
        "emp_code": "EMP987006",
        "name": "Test Tester",
        "email": "000@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-15",
    }
    # Create or expect 409
    code, data = req("POST", "/employees", data=test_emp, expected_status=201)
    assert data["emp_code"] == "EMP9876"

    # Duplicate should 409
    code, err = req("POST", "/employees", data=test_emp, expected_status=409)
    assert "already exists" in err["detail"]


def test_list_employees():
    code, data = req("GET", "/employees?page=1&page_size=10", expected_status=200)
    assert "items" in data
    assert data["total"] > 0
    assert len(data["items"]) <= 10

    # Department filter
    code, filtered = req("GET", "/employees?department=Engineering", expected_status=200)
    for emp in filtered["items"]:
        assert emp["department"] == "Engineering"


# 3. Attendance API
def test_punch_in_and_out():
    # Using fixed timestamp in epoch ms: 2026-10-14 09:35:00 IST -> 04:05:00 UTC -> 1791950700000 ms
    # 2026-10-14 09:35:00 IST
    pi_ms = 1791950700000
    po_ms = 1791983100000  # 18:35:00 IST (+9 hours)

    # Punch in
    code, pi_data = req(
        "POST",
        "/attendance/punch-in",
        data={"emp_code": "EMP9876", "punched_at": pi_ms, "status": "PRESENT"},
        expected_status=201,
    )
    assert pi_data["emp_code"] == "EMP9876"
    assert pi_data["date"] == "2026-10-14"
    assert pi_data["late_minutes"] == 0

    # Duplicate punch-in on same day should 409
    code, err = req(
        "POST",
        "/attendance/punch-in",
        data={"emp_code": "EMP9876", "punched_at": pi_ms, "status": "PRESENT"},
        expected_status=409,
    )
    assert "already punched in" in err["detail"]

    # Punch out
    code, po_data = req(
        "POST",
        "/attendance/punch-out",
        data={"emp_code": "EMP9876", "punched_at": po_ms},
        expected_status=200,
    )
    assert po_data["work_hours"] == 9.0
    assert po_data["punch_out"] == po_ms

    # Duplicate punch-out should 404 (no open punch-in)
    code, err2 = req(
        "POST",
        "/attendance/punch-out",
        data={"emp_code": "EMP9876", "punched_at": po_ms},
        expected_status=404,
    )


def test_list_attendance():
    code, data = req("GET", "/attendance?page=1&page_size=5", expected_status=200)
    assert "items" in data
    assert data["total"] > 0

    # Filter by date and employee
    code, data2 = req(
        "GET",
        "/attendance?emp_code=EMP9876&date_from=2026-10-14&date_to=2026-10-14",
        expected_status=200,
    )
    assert len(data2["items"]) == 1
    assert data2["items"][0]["emp_code"] == "EMP9876"


def test_regularize_attendance():
    # Regularize EMP9876's 2026-10-14 record
    patch_body = {
        "status": "WFH",
        "reason": "WFH approval retroactive update",
        "regularized_by": "HR_ADMIN",
    }
    code, data = req("PATCH", "/attendance/EMP9876/2026-10-14", data=patch_body, expected_status=200)
    assert data["status"] == "WFH"
    assert len(data["history"]) >= 1
    assert data["history"][-1]["by"] == "HR_ADMIN"
    assert data["history"][-1]["changes"]["status"]["to"] == "WFH"


# 4. Analytics API
def test_employee_monthly_analytics():
    # Check EMP0001 for 2026-07
    code, data = req("GET", "/analytics/employees/EMP0001/monthly?month=2026-07", expected_status=200)
    assert data["emp_code"] == "EMP0001"
    assert data["month"] == "2026-07"
    assert "working_days" in data
    assert "present_days" in data
    assert "attendance_pct" in data


def test_departments_summary_analytics():
    code, data = req("GET", "/analytics/departments/summary?month=2026-07", expected_status=200)
    assert data["month"] == "2026-07"
    assert "items" in data
    assert len(data["items"]) > 0
    dept_names = [d["department"] for d in data["items"]]
    assert "Engineering" in dept_names

    # Filter specific department
    code, dept_data = req(
        "GET", "/analytics/departments/summary?month=2026-07&department=Engineering", expected_status=200
    )
    assert len(dept_data["items"]) == 1
    assert dept_data["items"][0]["department"] == "Engineering"


def test_late_leaderboard_analytics():
    code, data = req("GET", "/analytics/leaderboard/late?month=2026-07&limit=5", expected_status=200)
    assert data["month"] == "2026-07"
    assert "items" in data
    for item in data["items"]:
        assert "rank" in item
        assert "emp_code" in item
        assert "total_late_minutes" in item


def test_department_trend_analytics():
    code, data = req(
        "GET", "/analytics/departments/Engineering/trend?from=2026-07-01&to=2026-07-15", expected_status=200
    )
    assert data["department"] == "Engineering"
    assert "items" in data
    assert len(data["items"]) == 15
    for row in data["items"]:
        assert "date" in row
        assert "is_working_day" in row
        assert "headcount" in row
        assert "attendance_rate" in row


# 5. Admin Explain Diagnostics API
def test_explain_endpoints():
    endpoints = [
        "/admin/explain/attendance_list",
        "/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07",
        "/admin/explain/department_summary?month=2026-07",
        "/admin/explain/late_leaderboard?month=2026-07",
        "/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10",
    ]
    for ep in endpoints:
        code, data = req("GET", ep, expected_status=200)
        assert "explain" in data
        assert "collection" in data


def main():
    print("=" * 60)
    print("RUNNING API TEST SUITE AGAINST http://localhost:8000")
    print("=" * 60)

    test("1. GET /health", test_health)
    test("2. POST /employees (create + 409 duplicate check)", test_create_and_duplicate_employee)
    test("3. GET /employees (pagination + dept filter)", test_list_employees)
    test("4. POST /attendance/punch-in & punch-out", test_punch_in_and_out)
    test("5. GET /attendance (filtering & pagination)", test_list_attendance)
    test("6. PATCH /attendance/{emp_code}/{date} (regularize)", test_regularize_attendance)
    test("7. GET /analytics/employees/{emp_code}/monthly", test_employee_monthly_analytics)
    test("8. GET /analytics/departments/summary", test_departments_summary_analytics)
    test("9. GET /analytics/leaderboard/late", test_late_leaderboard_analytics)
    test("10. GET /analytics/departments/{dept}/trend", test_department_trend_analytics)
    test("11. GET /admin/explain/{endpoint}", test_explain_endpoints)

    print("=" * 60)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
