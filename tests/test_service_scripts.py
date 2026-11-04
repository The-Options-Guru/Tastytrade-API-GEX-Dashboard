import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parent.parent
PWSH = shutil.which("pwsh")


@unittest.skipUnless(os.name == "nt" and PWSH, "Windows service scripts require PowerShell 7")
class ServiceScriptTests(unittest.TestCase):
    def run_script(self, script):
        return subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=ROOT, capture_output=True, text=True, timeout=30,
        )

    def result(self, script):
        completed = self.run_script(
            "$ErrorActionPreference = 'Stop'; . .\\gex_services.ps1\n" + script
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        line = next(line for line in completed.stdout.splitlines() if line.startswith("RESULT:"))
        return json.loads(line.removeprefix("RESULT:"))

    def test_process_matching_excludes_foreign_interpreters_and_similar_commands(self):
        results = self.result(r'''
$results = foreach ($service in $GexServices) {
    $prefix = '"' + $GexPython + '" ' + $service.Module
    $full = Join-Path $GexRoot $service.Script
    $commands = @(
        ($prefix + '"' + $full + '"'),
        ($prefix + $service.Script),
        ($prefix + '"' + $full + '" --extra argument'),
        ('"C:\Other\python.exe" ' + $service.Module + '"' + $full + '"'),
        ($prefix + $service.Script + '.unrelated'),
        ('"' + $GexPython + '" -c "pass" ' + $service.Script),
        ($prefix + '"C:\Other\' + $service.Script + '"'),
        ($prefix + '"' + $full + '".unrelated')
    )
    foreach ($command in $commands) {
        Test-GexProcess ([pscustomobject]@{CommandLine = $command}) $service
    }
}
'RESULT:' + (ConvertTo-Json -InputObject @($results) -Compress)
''')
        self.assertEqual(results, [True, True, True, False, False, False, False, False] * 3)

    def test_http_success_alone_does_not_make_a_recorder_ready(self):
        results = self.result(r'''
function Invoke-WebRequest { return $script:response }
$results = foreach ($case in @(
    @{Code=200; State='paused'; Fresh=$true; Index=1},
    @{Code=200; State='recording'; Fresh=$true; Index=1},
    @{Code=200; State='listening'; Fresh=$true; Index=2},
    @{Code=200; State='listening'; Fresh=$false; Index=2},
    @{Code=200; State='failed'; Fresh=$true; Index=2},
    @{Code=503; State='listening'; Fresh=$true; Index=2}
)) {
    $script:response = [pscustomobject]@{
        StatusCode=$case.Code
        Content=(@{state=$case.State; heartbeat_fresh=$case.Fresh} | ConvertTo-Json)
    }
    (Get-GexHealth $GexServices[$case.Index]).Ready
}
$script:response = [pscustomobject]@{StatusCode=200; Content='not the expected application'}
$results += (Get-GexHealth $GexServices[0]).Ready
'RESULT:' + (ConvertTo-Json -InputObject @($results) -Compress)
''')
        self.assertEqual(results, [True, True, True, False, False, False, False])

    def test_stop_refuses_foreign_port_owner(self):
        completed = self.run_script(r'''
function Get-NetTCPConnection {
    [pscustomobject]@{LocalPort=8501; OwningProcess=12345}
}
function Get-CimInstance {
    [pscustomobject]@{ProcessId=12345; CommandLine='"C:\Other\python.exe" unrelated.py'}
}
function Stop-Process { throw 'UNSAFE_STOP' }
& .\stop_services.ps1
''')
        self.assertEqual(completed.returncode, 1)
        self.assertIn("unrecognized process", completed.stderr)
        self.assertNotIn("UNSAFE_STOP", completed.stdout + completed.stderr)

    def test_stop_rechecks_identity_before_terminating_a_pid(self):
        completed = self.run_script(r'''
function Get-NetTCPConnection { }
function Get-CimInstance {
    param($Filter)
    $process = [pscustomobject]@{
        ProcessId=12345; CreationDate='original'
        CommandLine=('"'+(Join-Path (Get-Location) '.venv\Scripts\python.exe')+'" history_recorder.py')
    }
    if ($Filter -like 'ProcessId*') { $process.CreationDate='reused-pid' }
    $process
}
function Stop-Process { throw 'UNSAFE_STOP' }
& .\stop_services.ps1
''')
        self.assertEqual(completed.returncode, 1)
        self.assertIn("changed ownership", completed.stderr)
        self.assertNotIn("UNSAFE_STOP", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
