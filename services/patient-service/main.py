import os
import time
from typing import Any

import psycopg
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from pydantic import BaseModel, EmailStr
from starlette.responses import Response

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medicalhp:medicalhp@localhost:5432/medicalhp")
SERVICE_NAME = os.getenv("SERVICE_NAME", "patient-service")

REQUESTS = Counter("medicalhp_http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("medicalhp_request_seconds", "HTTP request latency", ["service", "path"])

app = FastAPI(title="MedicalHP Patient Service")


class PatientCreate(BaseModel):
    full_name: str
    email: EmailStr
    phone: str


def db() -> psycopg.Connection[Any]:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


@app.middleware("http")
async def metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    REQUESTS.labels(SERVICE_NAME, request.method, path, str(response.status_code)).inc()
    LATENCY.labels(SERVICE_NAME, path).observe(time.perf_counter() - start)
    return response


@app.get("/health")
def health():
    with db() as conn:
        conn.execute("SELECT 1")
    return {"service": SERVICE_NAME, "status": "healthy"}


@app.get("/metrics")
def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@app.get("/patients")
def list_patients():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, full_name, email, phone, created_at FROM patients ORDER BY created_at DESC"
        ).fetchall()
    return {"patients": rows}


@app.post("/patients", status_code=201)
def create_patient(payload: PatientCreate):
    with db() as conn:
        row = conn.execute(
            """
            INSERT INTO patients (full_name, email, phone)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name, phone = EXCLUDED.phone
            RETURNING id, full_name, email, phone, created_at
            """,
            (payload.full_name, payload.email, payload.phone),
        ).fetchone()
        conn.commit()
    return row


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str):
    with db() as conn:
        row = conn.execute(
            "SELECT id, full_name, email, phone, created_at FROM patients WHERE id = %s",
            (patient_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="patient_not_found")
    return row
