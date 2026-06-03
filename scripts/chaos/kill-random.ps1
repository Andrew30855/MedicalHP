param(
  [string]$Service = "appointment-service"
)

$containers = docker compose ps -q $Service
if (-not $containers) {
  Write-Error "No hay contenedores activos para $Service"
  exit 1
}

$target = $containers | Get-Random
Write-Host "Matando contenedor $target del servicio $Service"
docker kill $target
Write-Host "Docker Compose debe levantar una replica nueva por restart: unless-stopped."
