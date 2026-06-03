# MedicalHP

MedicalHP es una plataforma distribuida para reservar citas medicas, laboratorios y especialidades. La demo esta pensada para probar alta disponibilidad, consistencia de slots, compensacion ante fallo de pago, mensajeria asincrona, observabilidad, carga y caos.

## Arquitectura

- `api-gateway`: punto unico de entrada, proxy a microservicios, rate limiting con Redis y metricas.
- `patient-service`: alta y consulta de pacientes.
- `doctor-schedule-service`: medicos, slots, cache Redis y control transaccional de cupos.
- `appointment-service`: idempotencia, reserva, pago simulado, confirmacion, compensacion y outbox.
- `notification-service`: consumidor RabbitMQ y persistencia de notificaciones.
- `postgres`: base principal con pacientes, doctores, slots, holds, citas, outbox y notificaciones.
- `redis`: cache de medicos y rate limiting.
- `rabbitmq`: eventos asincronos de citas confirmadas.
- `prometheus` y `grafana`: metricas tecnicas y de negocio.
- `frontend`: consola React para reservas, salud y trazabilidad.

## Ejecutar

```powershell
docker compose up --build --scale appointment-service=3 --scale doctor-schedule-service=2
```

URLs:

- Frontend: http://localhost:3000
- API Gateway: http://localhost:8080
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3001 (`admin` / `medicalhp`)
- RabbitMQ: http://localhost:15672 (`medicalhp` / `medicalhp`)

Acceso al frontend:

- Usuario: `André` | Contraseña: `andre2005`
- Usuario: `Edgar` | Contraseña: `Epala20`

## Flujo critico

1. El frontend llama `POST /api/appointments` con `Idempotency-Key`.
2. `appointment-service` crea la cita en estado `RESERVING`.
3. `doctor-schedule-service` toma un lock `SELECT ... FOR UPDATE` sobre el slot.
4. Si hay cupo, crea un hold temporal; si no, responde `409`.
5. Si el pago simulado falla, `appointment-service` libera el hold y marca `PAYMENT_FAILED`.
6. Si el pago pasa, confirma el hold, incrementa `confirmed_count` e inserta un evento en outbox.
7. El outbox publica en RabbitMQ y `notification-service` guarda la notificacion.

Reglas protegidas:

- No se duplica un slot horario por bloqueo transaccional y `CHECK (confirmed_count + held_count <= capacity)`.
- No se procesa doble la misma operacion por `idempotency_key UNIQUE`.
- No se pierde el evento critico porque queda en `appointment_outbox` si RabbitMQ falla.
- No se elimina historial; las citas cambian de estado.
- Las citas pueden cancelarse solo con motivo obligatorio; el sistema conserva el historial y libera el cupo si estaba confirmado.

## Prueba de carga

Instala k6 y ejecuta:

```powershell
k6 run ./scripts/load/k6-medicalhp.js
```

Configuracion por defecto: 1,000 req/s por 50 segundos, equivalente a 50,000 peticiones. Para bajar o subir:

```powershell
$env:RATE=500; $env:DURATION="100s"; k6 run ./scripts/load/k6-medicalhp.js
```

Metricas a reportar: `http_reqs`, throughput, `http_req_duration avg/p95/p99`, tasa de error, `medicalhp_business_success`, CPU/memoria de Docker y paneles Grafana.

## Prueba de caos

Mientras la carga corre, ejecuta una de estas acciones:

```powershell
powershell ./scripts/chaos/kill-random.ps1 -Service appointment-service
powershell ./scripts/chaos/restart-redis.ps1
powershell ./scripts/chaos/scale-appointments.ps1 -Replicas 1
powershell ./scripts/chaos/scale-appointments.ps1 -Replicas 3
```

Evidencia esperada:

- La API sigue respondiendo por replicas restantes o se recupera por `restart: unless-stopped`.
- Redis puede reiniciarse y el gateway/schedule operan en modo degradado.
- RabbitMQ puede caer temporalmente sin perder eventos gracias al outbox.
- Los slots mantienen `confirmed_count + held_count <= capacity`.
- No aparecen citas duplicadas con la misma llave de idempotencia.

## Comandos utiles de verificacion

```powershell
curl http://localhost:8080/api/system/health
curl http://localhost:8080/api/doctors
curl http://localhost:8080/api/appointments
docker compose ps
docker compose logs appointment-service
```

Validar regla de negocio desde PostgreSQL:

```powershell
docker compose exec postgres psql -U medicalhp -d medicalhp -c "SELECT id, capacity, confirmed_count, held_count FROM slots WHERE confirmed_count + held_count > capacity;"
docker compose exec postgres psql -U medicalhp -d medicalhp -c "SELECT idempotency_key, count(*) FROM appointments GROUP BY idempotency_key HAVING count(*) > 1;"
```
