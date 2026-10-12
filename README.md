# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne technical assignment.

## Run locally

Install Python 3.11+, MongoDB 6+, and the dependencies:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment (or in a local, untracked `.env` file), then optionally
load the sample documents:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000
```

The readiness endpoint is `GET /health`. Interactive API documentation is available at `/docs`.

All API timestamps are epoch milliseconds. The service stores BSON datetimes in MongoDB, applies IST calendar
rules, creates indexes at startup, and uses MongoDB aggregation for analytics. `REVIEW.md` records the defects
found in the starter, `DECISIONS.md` explains the main design choices.

Do not commit `.env`, credentials, virtual environments, database dumps, or a Dockerfile.
