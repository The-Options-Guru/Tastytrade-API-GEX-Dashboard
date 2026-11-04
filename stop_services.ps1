#Requires -Version 7.0

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'gex_services.ps1')

$failed = $false
Write-Host 'Stopping project processes. An in-progress capture may be interrupted; saved data files are not deleted.'
foreach ($service in $GexServices) {
    try {
        Assert-GexPortOwnership $service
        $processes = @(Get-GexProcesses $service)
        if ($processes.Count -eq 0) {
            Write-Host "$($service.Name): already stopped."
            continue
        }
        foreach ($process in $processes) {
            $current = Get-CimInstance Win32_Process -Filter "ProcessId = $($process.ProcessId)" -ErrorAction Stop
            if ($null -eq $current) { continue }
            if ($current.CreationDate -ne $process.CreationDate -or -not (Test-GexProcess $current $service)) {
                throw "PID $($process.ProcessId) changed ownership. Refusing to stop it."
            }
            Stop-Process -Id $current.ProcessId -ErrorAction Stop
        }
        $deadline = (Get-Date).AddSeconds(10)
        do {
            $remaining = @(Get-GexProcesses $service)
            $listeners = @(Get-GexListeners $service)
            if ($remaining.Count -eq 0 -and $listeners.Count -eq 0) { break }
            Start-Sleep -Milliseconds 250
        } while ((Get-Date) -lt $deadline)
        if ($remaining.Count -gt 0 -or $listeners.Count -gt 0) {
            throw 'Could not confirm shutdown. Remaining processes/listeners were not forcibly replaced.'
        }
        Write-Host "$($service.Name): stopped; port $($service.Port) released."
    }
    catch {
        $failed = $true
        Write-Error "$($service.Name): $($_.Exception.Message)" -ErrorAction Continue
    }
}
if ($failed) { exit 1 }
