#Requires -Version 7.0

$GexRoot = $PSScriptRoot
$GexPython = Join-Path $GexRoot '.venv\Scripts\python.exe'
$GexServices = @(
    [pscustomobject]@{ Name = 'Dashboard'; Script = 'simple_dashboard.py'; Port = 8501; HealthPath = '_stcore/health'; Module = '-m streamlit run ' }
    [pscustomobject]@{ Name = 'History'; Script = 'history_recorder.py'; Port = 8504; HealthPath = 'health'; Module = '' }
    [pscustomobject]@{ Name = 'Flow'; Script = 'flow_recorder.py'; Port = 8502; HealthPath = 'health'; Module = '' }
)

function Test-GexProcess {
    param($Process, $Service)

    # Match the project interpreter and direct script invocation, never a name or port alone.
    $python = [regex]::Escape($GexPython)
    $absolute = [regex]::Escape((Join-Path $GexRoot $Service.Script))
    $relative = [regex]::Escape($Service.Script)
    $module = [regex]::Escape($Service.Module).Replace('\ ', '\s+')
    $pattern = '^\s*(?:"' + $python + '"|' + $python + ')\s+' + $module +
        '(?:"' + $absolute + '"|' + $absolute + '|"' + $relative + '"|' + $relative + ')(?:\s|$)'
    return $null -ne $Process -and $Process.CommandLine -imatch $pattern
}

function Get-GexProcesses {
    param($Service)

    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop |
        Where-Object { Test-GexProcess $_ $Service }
}

function Get-GexListeners {
    param($Service)

    Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $Service.Port }
}

function Assert-GexPortOwnership {
    param($Service)

    foreach ($listener in @(Get-GexListeners $Service)) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction Stop
        if (-not (Test-GexProcess $owner $Service)) {
            throw "Port $($Service.Port) belongs to an unrecognized process. Nothing on that port will be stopped or replaced."
        }
    }
}

function Get-GexHealth {
    param($Service)

    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$($Service.Port)/$($Service.HealthPath)" `
            -SkipHttpErrorCheck -TimeoutSec 3 -ErrorAction Stop
    }
    catch [System.Net.Http.HttpRequestException] {
        return [pscustomobject]@{ Ready = $false; Detail = 'HTTP connection unavailable' }
    }
    catch [System.Threading.Tasks.TaskCanceledException] {
        return [pscustomobject]@{ Ready = $false; Detail = 'HTTP request timed out' }
    }
    if ($response.StatusCode -ne 200) {
        return [pscustomobject]@{ Ready = $false; Detail = "HTTP $($response.StatusCode); check the local health endpoint and service logs" }
    }
    if ($Service.Name -eq 'Dashboard') {
        return [pscustomobject]@{ Ready = $response.Content.Trim() -eq 'ok'; Detail = 'Streamlit health response' }
    }
    $health = $response.Content | ConvertFrom-Json -ErrorAction Stop
    $states = if ($Service.Name -eq 'History') { @('recording', 'collecting', 'paused') } else { @('listening', 'paused') }
    return [pscustomobject]@{
        Ready = $health.heartbeat_fresh -eq $true -and $health.state -in $states
        Detail = "Recorder self-reported state: $($health.state); fresh heartbeat: $($health.heartbeat_fresh)"
    }
}
