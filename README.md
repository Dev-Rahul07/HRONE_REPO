# Employee Attendance & Analytics API

A high-performance, production-ready Attendance & Analytics backend service built with **FastAPI** and **MongoDB**, implementing comprehensive business logic (R1–R10) for shift tracking, attendance regularization, audit history, and department analytics.

---

## Tech Stack

- **Framework**: FastAPI (Python 3.10+)
- **Database**: MongoDB 6.0+ / 7.0 (via PyMongo with timezone awareness)
- **Validation**: Pydantic v2
- **Server**: Uvicorn ASGI

---

## Core Features & Business Logic (R1–R10)

- **R1 IST & Attendance Date**: Datetimes handled with UTC storage and IST (UTC+05:30) business boundaries. Overnight shifts (`shift_end <= shift_start`) mapping early morning punches back to previous calendar day. Whole-second truncation for all calculations.
- **R2 Late Arrival Tracking**: 10-minute grace period with strict threshold (`> 600s`). Late minutes counted as floor minutes elapsed from `shift_start`.
- **R3 Overtime Calculation**: Whole-minute overtime floor after `shift_end`, activated once reaching the 30-minute threshold.
- **R4 & R8 Work Hours & Rounding**: Exact duration divided by 3600, using `Decimal` standard half-up rounding (`ROUND_HALF_UP`) instead of Banker's rounding.
- **R5 Half-Day Flag**: Marks half day if rounded `work_hours < 4.50`, counted as `0.5` present day.
- **R6 Presence Categories**: `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **R7 Working Days**: Monday–Friday calendar count. Accurate handling for mid-month joiners (`max(first_day, joined_on)`).
- **R9 Department Headcount**: Comprehensive employee census including employees with zero attendance records for the month.
- **Concurrency & Races**: Compound unique index `(emp_code, date)` prevents duplicate punch-in race conditions (409 Conflict). Atomic CAS update prevents concurrent punch-out and regularize conflicts.
- **Analytics & Diagnostics**: MongoDB `$rank` standard competition ranking for late leaderboards, `$densify` for gap-free daily trends, and `/admin/explain/{endpoint}` for execution plans.

---

## Project Structure

```
├── app/
│   └── main.py            # Complete application (models, routes, helpers, pipeline builders)
├── sample_data/           # Sample MongoDB Extended JSON datasets
├── sample_seed.py         # Database seeding script
├── test_all_apis.py       # Automated test suite covering all 11 endpoints
├── openapi.yaml           # API specification contract
├── DATA_MODEL.md          # Database schema and document specifications
├── REVIEW.md              # Documented starter defects and solutions
├── DECISIONS.md           # Architecture, indexing, and design decisions
├── requirements.txt       # Project dependencies
└── README.md              # Project documentation
```

---

## Local Setup & Run

### 1. Prerequisites
- Python 3.10+
- MongoDB 6.0+ running locally on port 27017 or MongoDB Atlas connection

### 2. Environment Setup
```bash
# Clone the repository
git clone <repo_url>
cd HRONE_REPO

# Create and activate virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Environment Variables
Create a `.env` file in the project root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 4. Seed Sample Data
```bash
python sample_seed.py
```

### 5. Start the Application
```bash
uvicorn app.main:app --port 8000 --reload
```
API Documentation will be available at:
- **Interactive Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`

---

## Testing

An automated test suite is included to verify all endpoints, error handling, and business logic:

```bash
python test_all_apis.py
```

### Endpoints Covered:
1. `GET /health` - MongoDB connectivity check
2. `POST /employees` - Employee registration & 409 duplicate check
3. `GET /employees` - Paginated employee directory & filters
4. `POST /attendance/punch-in` - Punch-in & same-day duplicate guard
5. `POST /attendance/punch-out` - Atomic punch-out & work hours calculation
6. `GET /attendance` - Attendance logs with date range & employee filters
7. `PATCH /attendance/{emp_code}/{date}` - Regularization & audit history log
8. `GET /analytics/employees/{emp_code}/monthly` - Employee monthly metrics
9. `GET /analytics/departments/summary` - Department monthly summary
10. `GET /analytics/leaderboard/late` - Late rank leaderboard ($rank competition ties)
11. `GET /analytics/departments/{dept}/trend` - Daily trend & 7-day moving average
12. `GET /admin/explain/{endpoint}` - MongoDB execution plans & explain stats
