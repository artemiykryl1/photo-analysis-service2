# TASK-003 (tasks/TASK-003/20_design.md §9.5): convenience wrapper around
# the four `kubectl port-forward` commands needed for the local demo.
# OPTIONAL - not a required step (design §9.5 "не как обязательный шаг");
# running the four commands by hand, each in its own terminal, works
# exactly the same and makes it obvious which forward is which while
# debugging. Ctrl+C in the terminal that ran this script stops all four
# (they are child jobs of this script's PowerShell process).
#
# Usage (from photo-service/):
#   pwsh k8s/port-forward.ps1
#   # ... use the UI/API/Grafana ...
#   # Ctrl+C to stop everything.

$ErrorActionPreference = "Stop"
$namespace = "photo-service"

Write-Host "Starting kubectl port-forward jobs for namespace '$namespace' ..."

$jobs = @(
    Start-Job -ScriptBlock { kubectl -n $using:namespace port-forward svc/web 8080:80 }
    Start-Job -ScriptBlock { kubectl -n $using:namespace port-forward svc/api 8000:8000 }
    Start-Job -ScriptBlock { kubectl -n $using:namespace port-forward svc/grafana 3000:3000 }
    Start-Job -ScriptBlock { kubectl -n $using:namespace port-forward svc/prometheus 9090:9090 }
)

Write-Host ""
Write-Host "Web UI:    http://localhost:8080"
Write-Host "API/docs:  http://localhost:8000/docs"
Write-Host "Grafana:   http://localhost:3000"
Write-Host "Prometheus http://localhost:9090"
Write-Host ""
Write-Host "Press Ctrl+C to stop all port-forwards."

try {
    while ($true) {
        Start-Sleep -Seconds 2
    }
}
finally {
    Write-Host "Stopping port-forward jobs..."
    $jobs | Stop-Job -PassThru | Remove-Job
}
