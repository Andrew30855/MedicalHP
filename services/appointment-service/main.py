import json
import os
import threading
import time
import uuid
from typing import Any

import httpx
import pika
import psycopg
from fastapi import FastAPI, Header, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from starlette.responses import Response

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medicalhp:medicalhp@localhost:5432/medicalhp")
SCHEDULE_SERVICE_URL = os.getenv("SCHEDULE_SERVICE_URL", "http://localhost:8002")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://medicalhp:medicalhp@localhost:5672/")
SERVICE_NAME = os.getenv("SERVICE_NAME", "appointment-service")
QUEUE_NAME = "medicalhp.notifications"

REQUESTS = Counter("medicalhp_http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("medicalhp_request_seconds", "HTTP request latency", ["service", "path"])
APPOINTMENTS = Counter("medicalhp_appointments_total", "Appointments by final status", ["service", "status"])
OUTBOX_PUBLISHED = Counter("medicalhp_outbox_published_total", "Published outbox messages", ["service"])
OUTBOX_FAILED = Counter("medicalhp_outbox_failed_total", "Failed outbox publishes", ["service"])

app = FastAPI(title="MedicalHP Appointment Service")


class AppointmentRequest(BaseModel):
    patient_id: str
    doctor_id: str
    slot_id: str
    amount_cents: int = Field(default=25000, ge=0)
    simulate_payment_failure: bool = False


class CancellationRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=500)
    requested_by: str = Field(min_length=2, max_length=80)


def db() -> psycopg.Connection[Any]:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def ensure_schema():
    with db() as conn:
        conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancellation_reason TEXT")
        conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_by TEXT")
        conn.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ")
        conn.commit()


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
        params = pika.URLParameters(RABBITMQ_URL)
        connection = pika.BlockingConnection(params)
        connection.close()
    except Exception:
        degraded.append("rabbitmq")
    return {"service": SERVICE_NAME, "status": "healthy", "degraded": degraded}


@app.get("/metrics")
def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


def publish_outbox_once(limit: int = 50):
    with db() as conn:
        events = conn.execute(
            """
            SELECT id, event_type, payload
            FROM appointment_outbox
            WHERE published = false
            ORDER BY id
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
    if not events:
        return 0

    params = pika.URLParameters(RABBITMQ_URL)
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    channel.queue_declare(queue=QUEUE_NAME, durable=True)
    published = 0
    try:
        for event in events:
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE_NAME,
                body=json.dumps({"type": event["event_type"], "payload": event["payload"]}, default=str),
                properties=pika.BasicProperties(delivery_mode=2),
            )
            with db() as conn:
                conn.execute(
                    "UPDATE appointment_outbox SET published = true, published_at = now() WHERE id = %s",
                    (event["id"],),
                )
                conn.commit()
            OUTBOX_PUBLISHED.labels(SERVICE_NAME).inc()
            published += 1
    finally:
        connection.close()
    return published


def outbox_worker():
    while True:
        try:
            publish_outbox_once()
        except Exception:
            OUTBOX_FAILED.labels(SERVICE_NAME).inc()
        time.sleep(3)


@app.on_event("startup")
def startup():
    ensure_schema()
    threading.Thread(target=outbox_worker, daemon=True).start()


@app.get("/appointments")
def list_appointments():
    with db() as conn:
        rows = conn.execute(
            """
            SELECT a.id, a.patient_id, p.full_name AS patient_name, a.doctor_id, d.full_name AS doctor_name,
                   d.specialty, a.slot_id, s.starts_at, a.status, a.idempotency_key, a.trace_id,
                   a.cancellation_reason, a.cancelled_by, a.cancelled_at, a.created_at
            FROM appointments a
            JOIN patients p ON p.id = a.patient_id
            JOIN doctors d ON d.id = a.doctor_id
            JOIN slots s ON s.id = a.slot_id
            ORDER BY a.created_at DESC
            LIMIT 100
            """
        ).fetchall()
    return {"appointments": rows}


@app.post("/appointments", status_code=201)
def create_appointment(payload: AppointmentRequest, idempotency_key: str | None = Header(default=None)):
    idem = idempotency_key or str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    appointment_id = str(uuid.uuid4())

    with db() as conn:
        with conn.transaction():
            inserted = conn.execute(
                """
                INSERT INTO appointments (id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key, trace_id)
                VALUES (%s, %s, %s, %s, 'RESERVING', %s, %s, %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key, trace_id, created_at
                """,
                (
                    appointment_id,
                    payload.patient_id,
                    payload.doctor_id,
                    payload.slot_id,
                    payload.amount_cents,
                    idem,
                    trace_id,
                ),
            ).fetchone()
            if not inserted:
                existing = conn.execute(
                    """
                    SELECT id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key,
                           trace_id, cancellation_reason, cancelled_by, cancelled_at, created_at
                    FROM appointments
                    WHERE idempotency_key = %s
                    """,
                    (idem,),
                ).fetchone()
                return {"appointment": existing, "idempotent": True}

    try:
        with httpx.Client(timeout=5) as client:
            hold_response = client.post(
                f"{SCHEDULE_SERVICE_URL}/slots/{payload.slot_id}/hold",
                json={"appointment_id": appointment_id, "ttl_seconds": 600},
            )
            if hold_response.status_code == 409:
                with db() as conn:
                    conn.execute(
                        "UPDATE appointments SET status = 'CANCELLED', updated_at = now() WHERE id = %s",
                        (appointment_id,),
                    )
                    conn.commit()
                APPOINTMENTS.labels(SERVICE_NAME, "slot_unavailable").inc()
                raise HTTPException(status_code=409, detail="slot_unavailable")
            hold_response.raise_for_status()

            if payload.simulate_payment_failure:
                client.post(f"{SCHEDULE_SERVICE_URL}/holds/{appointment_id}/release")
                with db() as conn:
                    conn.execute(
                        "UPDATE appointments SET status = 'PAYMENT_FAILED', updated_at = now() WHERE id = %s",
                        (appointment_id,),
                    )
                    conn.commit()
                APPOINTMENTS.labels(SERVICE_NAME, "PAYMENT_FAILED").inc()
                raise HTTPException(status_code=402, detail="payment_failed_hold_released")

            confirm_response = client.post(f"{SCHEDULE_SERVICE_URL}/holds/{appointment_id}/confirm")
            confirm_response.raise_for_status()
    except HTTPException:
        raise
    except Exception as exc:
        with httpx.Client(timeout=3) as client:
            try:
                client.post(f"{SCHEDULE_SERVICE_URL}/holds/{appointment_id}/release")
            except Exception:
                pass
        with db() as conn:
            conn.execute(
                "UPDATE appointments SET status = 'CANCELLED', updated_at = now() WHERE id = %s",
                (appointment_id,),
            )
            conn.commit()
        APPOINTMENTS.labels(SERVICE_NAME, "CANCELLED").inc()
        raise HTTPException(status_code=503, detail="reservation_compensated") from exc

    with db() as conn:
        appointment = conn.execute(
            """
            UPDATE appointments
            SET status = 'CONFIRMED', updated_at = now()
            WHERE id = %s
            RETURNING id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key,
                      trace_id, cancellation_reason, cancelled_by, cancelled_at, created_at
            """,
            (appointment_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO appointment_outbox (aggregate_id, event_type, payload)
            VALUES (%s, 'AppointmentConfirmed', %s)
            """,
            (
                appointment_id,
                json.dumps(
                    {
                        "appointment_id": appointment_id,
                        "patient_id": payload.patient_id,
                        "doctor_id": payload.doctor_id,
                        "slot_id": payload.slot_id,
                        "trace_id": trace_id,
                    }
                ),
            ),
        )
        conn.commit()

    APPOINTMENTS.labels(SERVICE_NAME, "CONFIRMED").inc()
    try:
        publish_outbox_once(limit=10)
    except Exception:
        OUTBOX_FAILED.labels(SERVICE_NAME).inc()
    return {"appointment": appointment, "idempotent": False}


@app.delete("/appointments/{appointment_id}")
def cancel_appointment(appointment_id: str, payload: CancellationRequest):
    with db() as conn:
        with conn.transaction():
            appointment = conn.execute(
                """
                SELECT id, slot_id, status
                FROM appointments
                WHERE id = %s
                FOR UPDATE
                """,
                (appointment_id,),
            ).fetchone()
            if not appointment:
                raise HTTPException(status_code=404, detail="appointment_not_found")
            if appointment["status"] == "CANCELLED":
                row = conn.execute(
                    """
                    SELECT id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key,
                           trace_id, cancellation_reason, cancelled_by, cancelled_at, created_at
                    FROM appointments
                    WHERE id = %s
                    """,
                    (appointment_id,),
                ).fetchone()
                return {"appointment": row, "idempotent": True}

            conn.execute("SELECT id FROM slots WHERE id = %s FOR UPDATE", (appointment["slot_id"],))
            if appointment["status"] == "CONFIRMED":
                conn.execute(
                    """
                    UPDATE slots
                    SET confirmed_count = GREATEST(confirmed_count - 1, 0),
                        version = version + 1
                    WHERE id = %s
                    """,
                    (appointment["slot_id"],),
                )
            elif appointment["status"] == "RESERVING":
                conn.execute(
                    """
                    UPDATE slot_holds
                    SET status = 'RELEASED', updated_at = now()
                    WHERE appointment_id = %s AND status = 'HELD'
                    """,
                    (appointment_id,),
                )
                conn.execute(
                    """
                    UPDATE slots
                    SET held_count = GREATEST(held_count - 1, 0),
                        version = version + 1
                    WHERE id = %s
                    """,
                    (appointment["slot_id"],),
                )

            row = conn.execute(
                """
                UPDATE appointments
                SET status = 'CANCELLED',
                    cancellation_reason = %s,
                    cancelled_by = %s,
                    cancelled_at = now(),
                    updated_at = now()
                WHERE id = %s
                RETURNING id, patient_id, doctor_id, slot_id, status, amount_cents, idempotency_key,
                          trace_id, cancellation_reason, cancelled_by, cancelled_at, created_at
                """,
                (payload.reason.strip(), payload.requested_by.strip(), appointment_id),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO appointment_outbox (aggregate_id, event_type, payload)
                VALUES (%s, 'AppointmentCancelled', %s)
                """,
                (
                    appointment_id,
                    json.dumps(
                        {
                            "appointment_id": appointment_id,
                            "reason": payload.reason.strip(),
                            "requested_by": payload.requested_by.strip(),
                        }
                    ),
                ),
            )
    APPOINTMENTS.labels(SERVICE_NAME, "CANCELLED").inc()
    return {"appointment": row, "idempotent": False}
