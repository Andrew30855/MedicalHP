param(
  [int]$Replicas = 3
)

Write-Host "Escalando appointment-service a $Replicas replicas"
docker compose up -d --scale appointment-service=$Replicas
docker compose ps appointment-service
