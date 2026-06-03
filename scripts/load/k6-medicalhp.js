import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Trend, Rate } from 'k6/metrics'

export const options = {
  scenarios: {
    fifty_thousand_requests: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 1000),
      timeUnit: '1s',
      duration: __ENV.DURATION || '50s',
      preAllocatedVUs: Number(__ENV.VUS || 300),
      maxVUs: Number(__ENV.MAX_VUS || 1200),
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<1200', 'p(99)<2500'],
    medicalhp_business_success: ['rate>0.90'],
  },
}

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8080'
const patientIds = [
  '11111111-1111-1111-1111-111111111111',
  '22222222-2222-2222-2222-222222222222',
]
const doctorId = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
const hotSlotId = 'aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa'
const widerSlotId = 'aaaaaaaa-2222-4222-8222-aaaaaaaaaaaa'

const businessLatency = new Trend('medicalhp_business_latency')
const businessSuccess = new Rate('medicalhp_business_success')

function requestHeaders(name) {
  return {
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': `${name}-${exec.scenario.iterationInTest}`,
    },
  }
}

export default function () {
  const started = Date.now()
  const selector = exec.scenario.iterationInTest % 10
  let res

  if (selector < 3) {
    res = http.get(`${BASE_URL}/api/doctors/${doctorId}/slots`)
    businessSuccess.add(check(res, { 'slots visible': (r) => r.status === 200 }))
  } else if (selector < 6) {
    res = http.get(`${BASE_URL}/api/appointments`)
    businessSuccess.add(check(res, { 'history visible': (r) => r.status === 200 }))
  } else {
    const slotId = selector === 9 ? widerSlotId : hotSlotId
    const payload = JSON.stringify({
      patient_id: patientIds[exec.scenario.iterationInTest % patientIds.length],
      doctor_id: doctorId,
      slot_id: slotId,
      amount_cents: 25000,
      simulate_payment_failure: selector === 8,
    })
    res = http.post(`${BASE_URL}/api/appointments`, payload, requestHeaders(`load-${slotId}`))
    businessSuccess.add(
      check(res, {
        'business result is controlled': (r) => [201, 402, 409, 503].includes(r.status),
      }),
    )
  }

  businessLatency.add(Date.now() - started)
  sleep(0.01)
}
