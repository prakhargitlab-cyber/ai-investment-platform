$ErrorActionPreference = "Stop"

$NS  = "ai-investment"
$TAG = "ibkr-multiuser-3h-k8svalidation-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$IMG = "ai-investment-registry:5000/broker-service:$TAG"

Write-Host "=== PACKAGE ==="
mvn -pl broker-service -am package
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== BUILD IMAGE ==="
docker build -t $IMG .\broker-service
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== IMPORT IMAGE ==="
k3d image import $IMG -c ai-investment-dev
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== DEPLOY BROKER ONLY ==="
kubectl set image deployment/broker-service broker-service=$IMG -n $NS
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

kubectl rollout status deployment/broker-service -n $NS --timeout=300s
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "=== DEPLOYED IMAGE ==="
kubectl get deployment broker-service -n $NS `
  -o jsonpath='{.spec.template.spec.containers[0].image}'

Write-Host ""
