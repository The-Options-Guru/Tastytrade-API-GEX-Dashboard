#Requires -Version 7.0

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'gex_services.ps1')

if (-not (Test-Path -LiteralPath $GexPython -PathType Leaf)) {
    throw 'Project Python is missing. Set up .venv and install requirements.txt first.'
}
foreach ($service in $GexServices) {
    if (-not (Test-Path -LiteralPath (Join-Path $GexRoot $service.Script) -PathType Leaf)) {
        throw "Missing service script: $($service.Script)"
    }
    Assert-GexPortOwnership $service
}

$logs = Join-Path $GexRoot 'flow_data\service_logs'
New-Item -ItemType Directory -Path $logs -Force | Out-Null
$failed = $false
$oldEncoding = $env:PYTHONIOENCODING
try {
    $env:PYTHONIOENCODING = 'utf-8'
    foreach ($service in $GexServices) {
        try {
            Assert-GexPortOwnership $service
            if (@(Get-GexProcesses $service).Count -gt 0) {
                Write-Host "$($service.Name): already running; checking readiness."
            }
            else {
                $arguments = $service.Module + '"' + (Join-Path $GexRoot $service.Script) + '"'
                if ($service.Name -eq 'Dashboard') {
                    $arguments += ' --server.address 127.0.0.1 --server.port 8501 --server.headless true --browser.gatherUsageStats false'
                }
                $logBase = Join-Path $logs "$($service.Name.ToLower())-$(Get-Date -Format 'yyyyMMdd-HHmmss-fff')"
                Start-Process -FilePath $GexPython -ArgumentList $arguments -WorkingDirectory $GexRoot `
                    -WindowStyle Hidden -RedirectStandardOutput "$logBase-out.log" `
                    -RedirectStandardError "$logBase-error.log" -ErrorAction Stop | Out-Null
                Write-Host "$($service.Name): launched. Logs: $logBase-*.log"
            }

            $deadline = (Get-Date).AddSeconds(60)
            do {
                if (@(Get-GexProcesses $service).Count -eq 0) {
                    throw 'Process exited before becoming ready. Check flow_data\service_logs.'
                }
                Assert-GexPortOwnership $service
                $health = Get-GexHealth $service
                if ($health.Ready) { break }
                Start-Sleep -Seconds 2
            } while ((Get-Date) -lt $deadline)
            if (-not $health.Ready) {
                throw "Not ready after 60 seconds: $($health.Detail). The process was left running for diagnosis/retry."
            }
            Write-Host "$($service.Name): ready on port $($service.Port). $($health.Detail)"
        }
        catch {
            $failed = $true
            Write-Error "$($service.Name): $($_.Exception.Message)" -ErrorAction Continue
        }
    }
}
finally {
    $env:PYTHONIOENCODING = $oldEncoding
}
if ($failed) { exit 1 }
Write-Host 'Dashboard: http://localhost:8501'
Write-Host 'Services run in the background until stopped or Windows shuts down. Reboot autostart is not configured.'
