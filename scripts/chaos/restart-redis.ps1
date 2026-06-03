Write-Host "Reiniciando Redis para validar degradacion controlada de cache/rate limit"
docker compose restart redis
Write-Host "Redis reiniciado. Verifica /api/system/health y Grafana."
