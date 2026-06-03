CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS patients (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  phone TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS doctors (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  full_name TEXT NOT NULL,
  specialty TEXT NOT NULL,
  room TEXT NOT NULL,
  active BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS slots (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  doctor_id UUID NOT NULL REFERENCES doctors(id),
  starts_at TIMESTAMPTZ NOT NULL,
  capacity INTEGER NOT NULL CHECK (capacity > 0),
  confirmed_count INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0),
  held_count INTEGER NOT NULL DEFAULT 0 CHECK (held_count >= 0),
  version INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (doctor_id, starts_at),
  CHECK (confirmed_count + held_count <= capacity)
);

CREATE TABLE IF NOT EXISTS slot_holds (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  slot_id UUID NOT NULL REFERENCES slots(id),
  appointment_id UUID NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('HELD', 'CONFIRMED', 'RELEASED')),
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS appointments (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  patient_id UUID NOT NULL REFERENCES patients(id),
  doctor_id UUID NOT NULL REFERENCES doctors(id),
  slot_id UUID NOT NULL REFERENCES slots(id),
  status TEXT NOT NULL CHECK (status IN ('RESERVING', 'CONFIRMED', 'PAYMENT_FAILED', 'CANCELLED')),
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
  idempotency_key TEXT NOT NULL UNIQUE,
  trace_id TEXT NOT NULL,
  cancellation_reason TEXT,
  cancelled_by TEXT,
  cancelled_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancellation_reason TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_by TEXT;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS appointment_outbox (
  id BIGSERIAL PRIMARY KEY,
  aggregate_id UUID NOT NULL,
  event_type TEXT NOT NULL,
  payload JSONB NOT NULL,
  published BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY,
  appointment_id UUID NOT NULL,
  channel TEXT NOT NULL,
  destination TEXT NOT NULL,
  status TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO patients (id, full_name, email, phone)
VALUES
  ('11111111-1111-1111-1111-111111111111', 'Ana Martinez', 'ana@medicalhp.test', '+502 5550 0101'),
  ('22222222-2222-2222-2222-222222222222', 'Carlos Rivera', 'carlos@medicalhp.test', '+502 5550 0202')
ON CONFLICT (email) DO NOTHING;

INSERT INTO doctors (id, full_name, specialty, room)
VALUES
  ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Dra. Valeria Soto', 'Cardiologia', 'A-204'),
  ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'Dr. Mateo Luna', 'Laboratorio clinico', 'Lab-01'),
  ('cccccccc-cccc-cccc-cccc-cccccccccccc', 'Dra. Emilia Paz', 'Medicina interna', 'B-110')
ON CONFLICT (id) DO NOTHING;

INSERT INTO slots (id, doctor_id, starts_at, capacity)
VALUES
  ('aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '2026-06-04T15:00:00Z', 1),
  ('aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', '2026-06-04T16:00:00Z', 2),
  ('bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', '2026-06-04T15:00:00Z', 3),
  ('cccccccc-1111-4111-8111-cccccccccccc', 'cccccccc-cccc-cccc-cccc-cccccccccccc', '2026-06-05T14:30:00Z', 1)
ON CONFLICT (doctor_id, starts_at) DO NOTHING;
