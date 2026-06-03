import json
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import redis
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from starlette.responses import Response

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medicalhp:medicalhp@localhost:5432/medicalhp")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_NAME = os.getenv("SERVICE_NAME", "doctor-schedule-service")

REQUESTS = Counter("medicalhp_http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("medicalhp_request_seconds", "HTTP request latency", ["service", "path"])
HOLDS = Counter("medicalhp_slot_holds_total", "Successful temporary slot holds", ["service"])
CONFLICTS = Counter("medicalhp_slot_conflicts_total", "Rejected slot reservations", ["service", "reason"])

app = FastAPI(title="MedicalHP Doctor Schedule Service")


class HoldRequest(BaseModel):
    appointment_id: str
    ttl_seconds: int = Field(default=600, ge=30, le=1800)


def db() -> psycopg.Connection[Any]:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def cache():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1)


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
    degraded = []
    with db() as conn:
        conn.execute("SELECT 1")
    try:
        cache().ping()
    except Exception:
        degraded.append("redis")
    return {"service": SERVICE_NAME, "status": "healthy", "degraded": degraded}


@app.get("/metrics")
def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


def release_expired_holds(conn: psycopg.Connection[Any], slot_id: str | None = None):
    if slot_id:
        expired = conn.execute(
            """
            UPDATE slot_holds
            SET status = 'RELEASED', updated_at = now()
            WHERE slot_id = %s AND status = 'HELD' AND expires_at < now()
            RETURNING slot_id
            """,
            (slot_id,),
        ).fetchall()
    else:
        expired = conn.execute(
            """
            UPDATE slot_holds
            SET status = 'RELEASED', updated_at = now()
            WHERE status = 'HELD' AND expires_at < now()
            RETURNING slot_id
            """
        ).fetchall()
    by_slot: dict[str, int] = {}
    for row in expired:
        by_slot[str(row["slot_id"])] = by_slot.get(str(row["slot_id"]), 0) + 1
    for expired_slot_id, count in by_slot.items():
        conn.execute(
            "UPDATE slots SET held_count = GREATEST(held_count - %s, 0), version = version + 1 WHERE id = %s",
            (count, expired_slot_id),
        )


@app.get("/doctors")
def list_doctors():
    cache_key = "medicalhp:doctors:v1"
    try:
        cached = cache().get(cache_key)
        if cached:
            return {"doctors": json.loads(cached), "cache": "hit"}
    except Exception:
        pass

    with db() as conn:
        rows = conn.execute(
            "SELECT id, full_name, specialty, room, active FROM doctors WHERE active = true ORDER BY specialty, full_name"
        ).fetchall()

    try:
        cache().setex(cache_key, 30, json.dumps(rows, default=str))
    except Exception:
        return {"doctors": rows, "cache": "degraded"}
    return {"doctors": rows, "cache": "miss"}


@app.get("/doctors/{doctor_id}/slots")
def list_slots(doctor_id: str):
    with db() as conn:
        release_expired_holds(conn)
        rows = conn.execute(
            """
            SELECT id, doctor_id, starts_at, capacity, confirmed_count, held_count,
                   capacity - confirmed_count - held_count AS available
            FROM slots
            WHERE doctor_id = %s
            ORDER BY starts_at
            """,
            (doctor_id,),
        ).fetchall()
        conn.commit()
    return {"slots": rows}


@app.post("/slots/{slot_id}/hold")
def hold_slot(slot_id: str, payload: HoldRequest):
    expires_at = datetime.now(UTC) + timedelta(seconds=payload.ttl_seconds)
    with db() as conn:
        try:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT * FROM slot_holds WHERE appointment_id = %s FOR UPDATE",
                    (payload.appointment_id,),
                ).fetchone()
                if existing:
                    return {"hold": existing, "idempotent": True}

                release_expired_holds(conn, slot_id)
                slot = conn.execute(
                    """
                    SELECT id, doctor_id, starts_at, capacity, confirmed_count, held_count
                    FROM slots
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (slot_id,),
                ).fetchone()
                if not slot:
                    raise HTTPException(status_code=404, detail="slot_not_found")

                if slot["confirmed_count"] + slot["held_count"] >= slot["capacity"]:
                    CONFLICTS.labels(SERVICE_NAME, "sold_out").inc()
                    raise HTTPException(status_code=409, detail="slot_unavailable")

                hold = conn.execute(
                    """
                    INSERT INTO slot_holds (slot_id, appointment_id, status, expires_at)
                    VALUES (%s, %s, 'HELD', %s)
                    RETURNING id, slot_id, appointment_id, status, expires_at
                    """,
                    (slot_id, payload.appointment_id, expires_at),
                ).fetchone()
                conn.execute(
                    "UPDATE slots SET held_count = held_count + 1, version = version + 1 WHERE id = %s",
                    (slot_id,),
                )
                HOLDS.labels(SERVICE_NAME).inc()
                return {"hold": hold, "idempotent": False}
        except HTTPException:
            raise
        except Exception as exc:
            CONFLICTS.labels(SERVICE_NAME, "transaction_error").inc()
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/holds/{appointment_id}/confirm")
def confirm_hold(appointment_id: str):
    with db() as conn:
        with conn.transaction():
            hold = conn.execute(
                "SELECT * FROM slot_holds WHERE appointment_id = %s FOR UPDATE",
                (appointment_id,),
            ).fetchone()
            if not hold:
                raise HTTPException(status_code=404, detail="hold_not_found")
            if hold["status"] == "CONFIRMED":
                return {"status": "CONFIRMED", "idempotent": True}
            if hold["status"] != "HELD" or hold["expires_at"] < datetime.now(UTC):
                CONFLICTS.labels(SERVICE_NAME, "hold_not_confirmable").inc()
                raise HTTPException(status_code=409, detail="hold_not_confirmable")
            conn.execute(
                "UPDATE slot_holds SET status = 'CONFIRMED', updated_at = now() WHERE appointment_id = %s",
                (appointment_id,),
            )
            conn.execute(
                """
                UPDATE slots
                SET held_count = GREATEST(held_count - 1, 0),
                    confirmed_count = confirmed_count + 1,
                    version = version + 1
                WHERE id = %s
                """,
                (hold["slot_id"],),
            )
    return {"status": "CONFIRMED", "idempotent": False}


@app.post("/holds/{appointment_id}/release")
def release_hold(appointment_id: str):
    with db() as conn:
        with conn.transaction():
            hold = conn.execute(
                "SELECT * FROM slot_holds WHERE appointment_id = %s FOR UPDATE",
                (appointment_id,),
            ).fetchone()
            if not hold:
                return {"status": "RELEASED", "idempotent": True}
            if hold["status"] != "HELD":
                return {"status": hold["status"], "idempotent": True}
            conn.execute(
                "UPDATE slot_holds SET status = 'RELEASED', updated_at = now() WHERE appointment_id = %s",
                (appointment_id,),
            )
            conn.execute(
                "UPDATE slots SET held_count = GREATEST(held_count - 1, 0), version = version + 1 WHERE id = %s",
                (hold["slot_id"],),
            )
    return {"status": "RELEASED", "idempotent": False}
