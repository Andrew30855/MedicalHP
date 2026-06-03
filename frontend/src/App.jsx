import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import {
  Alert,
  Badge,
  Button,
  Col,
  Form,
  Input,
  Layout,
  Progress,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import './App.css'
import clinicalImage from './assets/hero.png'

const { Header, Content, Sider } = Layout
const { Title, Text } = Typography

const api = axios.create({ baseURL: '/api', timeout: 15000 })

function fmtDate(value) {
  if (!value) return '-'
  return new Intl.DateTimeFormat('es-GT', {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value))
}

function statusColor(status) {
  if (status === 'CONFIRMED' || status === 'healthy') return 'green'
  if (status === 'RESERVING') return 'gold'
  if (status === 'PAYMENT_FAILED' || status === 'CANCELLED') return 'red'
  return 'blue'
}

function App() {
  const [patients, setPatients] = useState([])
  const [doctors, setDoctors] = useState([])
  const [slots, setSlots] = useState([])
  const [appointments, setAppointments] = useState([])
  const [notifications, setNotifications] = useState([])
  const [health, setHealth] = useState(null)
  const [selectedDoctor, setSelectedDoctor] = useState()
  const [loading, setLoading] = useState(false)
  const [form] = Form.useForm()

  const confirmedAppointments = appointments.filter((item) => item.status === 'CONFIRMED').length
  const unavailableSlots = slots.filter((slot) => Number(slot.available) <= 0).length
  const serviceHealth = Object.entries(health?.services ?? {})

  const selectedDoctorSlots = useMemo(
    () =>
      slots.map((slot) => ({
        label: `${fmtDate(slot.starts_at)} - cupos ${slot.available}/${slot.capacity}`,
        value: slot.id,
        disabled: Number(slot.available) <= 0,
      })),
    [slots],
  )

  async function loadCoreData() {
    setLoading(true)
    try {
      const [patientRes, doctorRes, appointmentRes, notificationRes, healthRes] = await Promise.all([
        api.get('/patients'),
        api.get('/doctors'),
        api.get('/appointments'),
        api.get('/notifications'),
        api.get('/system/health'),
      ])
      setPatients(patientRes.data.patients ?? [])
      setDoctors(doctorRes.data.doctors ?? [])
      setAppointments(appointmentRes.data.appointments ?? [])
      setNotifications(notificationRes.data.notifications ?? [])
      setHealth(healthRes.data)
      const firstDoctor = selectedDoctor || doctorRes.data.doctors?.[0]?.id
      if (firstDoctor) {
        setSelectedDoctor(firstDoctor)
        const slotRes = await api.get(`/doctors/${firstDoctor}/slots`)
        setSlots(slotRes.data.slots ?? [])
      }
    } catch (error) {
      message.error(error.response?.data?.detail ?? 'No se pudo cargar MedicalHP')
    } finally {
      setLoading(false)
    }
  }

  async function loadSlots(doctorId) {
    setSelectedDoctor(doctorId)
    form.setFieldValue('slot_id', undefined)
    const slotRes = await api.get(`/doctors/${doctorId}/slots`)
    setSlots(slotRes.data.slots ?? [])
  }

  async function createPatient(values) {
    const res = await api.post('/patients', values)
    setPatients((current) => [res.data, ...current.filter((patient) => patient.id !== res.data.id)])
    form.setFieldValue('patient_id', res.data.id)
    message.success('Paciente registrado')
  }

  async function reserveAppointment(values) {
    const key = `medicalhp-ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
    try {
      const res = await api.post('/appointments', values, {
        headers: { 'Idempotency-Key': key },
      })
      setAppointments((current) => [res.data.appointment, ...current])
      await loadSlots(values.doctor_id)
      message.success('Cita confirmada')
    } catch (error) {
      if (error.response?.status === 409) {
        message.warning('El slot ya no esta disponible')
      } else if (error.response?.status === 402) {
        await loadSlots(values.doctor_id)
        message.warning('Pago fallido; el cupo fue liberado')
      } else {
        message.error(error.response?.data?.detail ?? 'Reserva compensada')
      }
      await loadCoreData()
    }
  }

  useEffect(() => {
    const initialLoad = window.setTimeout(loadCoreData, 0)
    const interval = window.setInterval(loadCoreData, 8000)
    return () => {
      window.clearTimeout(initialLoad)
      window.clearInterval(interval)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const slotColumns = [
    { title: 'Inicio', dataIndex: 'starts_at', render: fmtDate },
    { title: 'Capacidad', dataIndex: 'capacity', align: 'right' },
    { title: 'Confirmadas', dataIndex: 'confirmed_count', align: 'right' },
    { title: 'Temporales', dataIndex: 'held_count', align: 'right' },
    {
      title: 'Disponible',
      dataIndex: 'available',
      align: 'right',
      render: (value) => <Tag color={Number(value) > 0 ? 'green' : 'red'}>{value}</Tag>,
    },
  ]

  const appointmentColumns = [
    { title: 'Paciente', dataIndex: 'patient_name' },
    { title: 'Medico', dataIndex: 'doctor_name' },
    { title: 'Especialidad', dataIndex: 'specialty' },
    { title: 'Inicio', dataIndex: 'starts_at', render: fmtDate },
    {
      title: 'Estado',
      dataIndex: 'status',
      render: (value) => <Tag color={statusColor(value)}>{value}</Tag>,
    },
    { title: 'Trace', dataIndex: 'trace_id', render: (value) => <Text code>{String(value).slice(0, 8)}</Text> },
  ]

  const notificationColumns = [
    { title: 'Cita', dataIndex: 'appointment_id', render: (value) => <Text code>{String(value).slice(0, 8)}</Text> },
    { title: 'Canal', dataIndex: 'channel' },
    { title: 'Destino', dataIndex: 'destination', render: (value) => <Text code>{String(value).slice(0, 8)}</Text> },
    { title: 'Estado', dataIndex: 'status', render: (value) => <Tag color="green">{value}</Tag> },
    { title: 'Creada', dataIndex: 'created_at', render: fmtDate },
  ]

  return (
    <Layout className="app-shell">
      <Sider className="side-panel" width={290} breakpoint="lg" collapsedWidth={0}>
        <div className="brand-block">
          <img src={clinicalImage} alt="MedicalHP clinical operations" />
          <div>
            <Title level={2}>MedicalHP</Title>
            <Text>Reservas medicas distribuidas</Text>
          </div>
        </div>
        <Space direction="vertical" size={14} className="side-metrics">
          <Statistic title="Citas confirmadas" value={confirmedAppointments} />
          <Statistic title="Slots sin cupo" value={unavailableSlots} />
          <Statistic title="Notificaciones" value={notifications.length} />
          <Progress
            percent={Math.round((serviceHealth.filter(([, svc]) => svc.status_code < 500).length / Math.max(serviceHealth.length, 1)) * 100)}
            size="small"
            strokeColor="#0b8f79"
          />
        </Space>
        <div className="runbook">
          <Text strong>Demo tecnica</Text>
          <Text code>docker compose up --build --scale appointment-service=3</Text>
          <Text code>powershell ./scripts/chaos/kill-random.ps1</Text>
          <Text code>k6 run ./scripts/load/k6-medicalhp.js</Text>
        </div>
      </Sider>

      <Layout>
        <Header className="topbar">
          <div>
            <Title level={1}>Consola de citas</Title>
            <Text>Gateway unico, slots consistentes, eventos trazables</Text>
          </div>
          <Space>
            <Badge status={serviceHealth.some(([, svc]) => svc.status_code >= 500) ? 'error' : 'success'} />
            <Button onClick={loadCoreData} loading={loading}>Actualizar</Button>
          </Space>
        </Header>

        <Content className="content">
          <Tabs
            defaultActiveKey="reserve"
            items={[
              {
                key: 'reserve',
                label: 'Reservas',
                children: (
                  <Row gutter={[16, 16]}>
                    <Col xs={24} xl={9}>
                      <section className="panel">
                        <Title level={3}>Nueva cita</Title>
                        <Form form={form} layout="vertical" onFinish={reserveAppointment}>
                          <Form.Item name="patient_id" label="Paciente" rules={[{ required: true }]}>
                            <Select
                              options={patients.map((patient) => ({ label: patient.full_name, value: patient.id }))}
                              placeholder="Seleccionar paciente"
                            />
                          </Form.Item>
                          <Form.Item name="doctor_id" label="Medico" rules={[{ required: true }]}>
                            <Select
                              value={selectedDoctor}
                              options={doctors.map((doctor) => ({
                                label: `${doctor.full_name} - ${doctor.specialty}`,
                                value: doctor.id,
                              }))}
                              onChange={(value) => {
                                form.setFieldValue('doctor_id', value)
                                loadSlots(value)
                              }}
                              placeholder="Seleccionar medico"
                            />
                          </Form.Item>
                          <Form.Item name="slot_id" label="Horario" rules={[{ required: true }]}>
                            <Select options={selectedDoctorSlots} placeholder="Seleccionar horario" />
                          </Form.Item>
                          <Form.Item name="amount_cents" label="Monto" initialValue={25000}>
                            <Input type="number" min={0} />
                          </Form.Item>
                          <Form.Item name="simulate_payment_failure" label="Simular pago fallido" valuePropName="checked">
                            <Switch />
                          </Form.Item>
                          <Button type="primary" htmlType="submit" block>
                            Reservar cita
                          </Button>
                        </Form>
                      </section>
                    </Col>
                    <Col xs={24} xl={15}>
                      <section className="panel">
                        <Title level={3}>Disponibilidad</Title>
                        <Table rowKey="id" columns={slotColumns} dataSource={slots} pagination={false} size="middle" />
                      </section>
                    </Col>
                    <Col xs={24}>
                      <section className="panel">
                        <Title level={3}>Historial intocable</Title>
                        <Table
                          rowKey="id"
                          columns={appointmentColumns}
                          dataSource={appointments}
                          pagination={{ pageSize: 6 }}
                          size="middle"
                        />
                      </section>
                    </Col>
                  </Row>
                ),
              },
              {
                key: 'patients',
                label: 'Pacientes',
                children: (
                  <Row gutter={[16, 16]}>
                    <Col xs={24} lg={8}>
                      <section className="panel">
                        <Title level={3}>Registro</Title>
                        <Form layout="vertical" onFinish={createPatient}>
                          <Form.Item name="full_name" label="Nombre" rules={[{ required: true }]}>
                            <Input />
                          </Form.Item>
                          <Form.Item name="email" label="Email" rules={[{ required: true, type: 'email' }]}>
                            <Input />
                          </Form.Item>
                          <Form.Item name="phone" label="Telefono" rules={[{ required: true }]}>
                            <Input />
                          </Form.Item>
                          <Button type="primary" htmlType="submit" block>
                            Guardar paciente
                          </Button>
                        </Form>
                      </section>
                    </Col>
                    <Col xs={24} lg={16}>
                      <section className="panel">
                        <Title level={3}>Pacientes</Title>
                        <Table
                          rowKey="id"
                          dataSource={patients}
                          pagination={false}
                          columns={[
                            { title: 'Nombre', dataIndex: 'full_name' },
                            { title: 'Email', dataIndex: 'email' },
                            { title: 'Telefono', dataIndex: 'phone' },
                          ]}
                        />
                      </section>
                    </Col>
                  </Row>
                ),
              },
              {
                key: 'observability',
                label: 'Observabilidad',
                children: (
                  <Row gutter={[16, 16]}>
                    <Col xs={24} lg={12}>
                      <section className="panel">
                        <Title level={3}>Servicios</Title>
                        <Space direction="vertical" className="service-list">
                          {serviceHealth.map(([name, svc]) => (
                            <div className="service-row" key={name}>
                              <Text strong>{name}</Text>
                              <Tag color={svc.status_code < 500 ? 'green' : 'red'}>{svc.status_code}</Tag>
                            </div>
                          ))}
                        </Space>
                        <Alert
                          type="info"
                          message="Grafana: http://localhost:3001 | Prometheus: http://localhost:9090 | RabbitMQ: http://localhost:15672"
                          showIcon
                        />
                      </section>
                    </Col>
                    <Col xs={24} lg={12}>
                      <section className="panel">
                        <Title level={3}>Eventos de notificacion</Title>
                        <Table
                          rowKey="id"
                          columns={notificationColumns}
                          dataSource={notifications}
                          pagination={{ pageSize: 6 }}
                          size="middle"
                        />
                      </section>
                    </Col>
                  </Row>
                ),
              },
            ]}
          />
        </Content>
      </Layout>
    </Layout>
  )
}

export default App
