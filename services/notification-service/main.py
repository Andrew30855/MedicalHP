import json
import os
import threading
import time
from typing import Any

import pika
import psycopg
from fastapi import FastAPI, Request
from prometheus_client import Counter, Histogram, generate_latest
from psycopg.rows import dict_row
from starlette.responses import Response

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://medicalhp:medicalhp@localhost:5432/medicalhp")
RABBITMQ_URL = os.getenv("RABBITMQ_URL", "amqp://medicalhp:medicalhp@localhost:5672/")
SERVICE_NAME = os.getenv("SERVICE_NAME", "notification-service")
QUEUE_NAME = "medicalhp.notifications"

REQUESTS = Counter("medicalhp_http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("medicalhp_request_seconds", "HTTP request latency", ["service", "path"])
NOTIFICATIONS = Counter("medicalhp_notifications_total", "Notifications persisted", ["service", "status"])

consumer_state = {"running": False, "last_error": None, "messages": 0}
app = FastAPI(title="MedicalHP Notification Service")


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
    degraded = []
    with db() as conn:
        conn.execute("SELECT 1")
    if not consumer_state["running"]:
        degraded.append("rabbitmq_consumer")
    return {"service": SERVICE_NAME, "status": "healthy", "degraded": degraded, "consumer": consumer_state}


@app.get("/metrics")
def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@app.get("/notifications")
def list_notifications():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, appointment_id, channel, destination, status, payload, created_at FROM notifications ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return {"notifications": rows, "consumer": consumer_state}


def handle_message(channel, method, properties, body):
    event = json.loads(body)
    payload = event.get("payload", {})
    appointment_id = payload.get("appointment_id")
    destination = payload.get("patient_id", "unknown-patient")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO notifications (appointment_id, channel, destination, status, payload)
            VALUES (%s, 'email', %s, 'SENT', %s)
            """,
            (appointment_id, destination, json.dumps(event)),
        )
        conn.commit()
    consumer_state["messages"] += 1
    NOTIFICATIONS.labels(SERVICE_NAME, "SENT").inc()
    channel.basic_ack(delivery_tag=method.delivery_tag)


def consume_forever():
    while True:
        try:
            params = pika.URLParameters(RABBITMQ_URL)
            connection = pika.BlockingConnection(params)
            channel = connection.channel()
            channel.queue_declare(queue=QUEUE_NAME, durable=True)
            channel.basic_qos(prefetch_count=10)
            channel.basic_consume(queue=QUEUE_NAME, on_message_callback=handle_message)
            consumer_state["running"] = True
            consumer_state["last_error"] = None
            channel.start_consuming()
        except Exception as exc:
            consumer_state["running"] = False
            consumer_state["last_error"] = str(exc)
            time.sleep(3)


@app.on_event("startup")
def startup():
    threading.Thread(target=consume_forever, daemon=True).start()
