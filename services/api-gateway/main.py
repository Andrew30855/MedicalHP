import os
import time
from typing import Any

import httpx
import redis
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Counter, Histogram, generate_latest
from starlette.responses import JSONResponse, Response

SERVICE_NAME = os.getenv("SERVICE_NAME", "api-gateway")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/1")
SERVICES = {
    "patients": os.getenv("PATIENT_SERVICE_URL", "http://localhost:8001"),
    "doctors": os.getenv("SCHEDULE_SERVICE_URL", "http://localhost:8002"),
    "appointments": os.getenv("APPOINTMENT_SERVICE_URL", "http://localhost:8003"),
    "notifications": os.getenv("NOTIFICATION_SERVICE_URL", "http://localhost:8004"),
}

REQUESTS = Counter("medicalhp_http_requests_total", "HTTP requests", ["service", "method", "path", "status"])
LATENCY = Histogram("medicalhp_request_seconds", "HTTP request latency", ["service", "path"])
RATE_LIMITS = Counter("medicalhp_rate_limited_total", "Gateway rate limit rejections", ["service"])

app = FastAPI(title="MedicalHP API Gateway")


def redis_client():
    return redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=1)


@app.middleware("http")
async def metrics(request: Request, call_next):
    start = time.perf_counter()
    response = await call_next(request)
    path = request.url.path
    REQUESTS.labels(SERVICE_NAME, request.method, path, str(response.status_code)).inc()
    LATENCY.labels(SERVICE_NAME, path).observe(time.perf_counter() - start)
    return response


async def enforce_rate_limit(request: Request):
    client = request.client.host if request.client else "unknown"
    bucket = int(time.time() // 60)
    key = f"medicalhp:gateway:rate:{client}:{bucket}"
    try:
        r = redis_client()
        count = r.incr(key)
        if count == 1:
            r.expire(key, 90)
        if count > 3000:
            RATE_LIMITS.labels(SERVICE_NAME).inc()
            raise HTTPException(status_code=429, detail="rate_limit_exceeded")
    except HTTPException:
        raise
    except Exception:
        request.state.gateway_degraded = "redis_rate_limit"


def service_for(path: str) -> tuple[str, str]:
    parts = path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "api":
        raise HTTPException(status_code=404, detail="route_not_found")
    resource = parts[1]
    if resource not in SERVICES:
        raise HTTPException(status_code=404, detail="service_not_found")
    upstream_path = "/" + "/".join(parts[1:])
    return SERVICES[resource], upstream_path


@app.get("/health")
def health():
    degraded = []
    try:
        redis_client().ping()
    except Exception:
        degraded.append("redis")
    return {"service": SERVICE_NAME, "status": "healthy", "degraded": degraded}


@app.get("/metrics")
def prometheus_metrics():
    return Response(generate_latest(), media_type="text/plain; version=0.0.4")


@app.get("/api/system/health")
async def system_health():
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=2) as client:
        for name, base_url in SERVICES.items():
            try:
                response = await client.get(f"{base_url}/health")
                results[name] = {"status_code": response.status_code, "body": response.json()}
            except Exception as exc:
                results[name] = {"status_code": 503, "error": str(exc)}
    return {"gateway": {"status": "healthy"}, "services": results}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    await enforce_rate_limit(request)
    base_url, upstream_path = service_for(request.url.path)
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length"}
    }
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            upstream = await client.request(
                request.method,
                f"{base_url}{upstream_path}",
                params=request.query_params,
                content=body,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="upstream_timeout") from exc
        except Exception as exc:
            raise HTTPException(status_code=503, detail="upstream_unavailable") from exc

    try:
        content = upstream.json()
    except Exception:
        content = {"raw": upstream.text}
    response = JSONResponse(content=content, status_code=upstream.status_code)
    if hasattr(request.state, "gateway_degraded"):
        response.headers["X-MedicalHP-Degraded"] = request.state.gateway_degraded
    return response
