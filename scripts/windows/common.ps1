# Shared helpers for the Windows install scripts (dot-sourced).

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# A Python 3.11 or newer: $env:PHONEUSE_PYTHON, the py launcher, python on the PATH
# (not the Microsoft Store stub), or a python.org install in the usual places.
function Find-Python {
    # A candidate that writes to stderr ("No suitable Python runtime") must not end the
    # search: Windows PowerShell 5.1 turns redirected native stderr into a terminating
    # error under the callers' "Stop".
    $ErrorActionPreference = "Continue"
    $candidates = @()
    if ($env:PHONEUSE_PYTHON) { $candidates += $env:PHONEUSE_PYTHON }
    $launcher = (Get-Command py -ErrorAction SilentlyContinue).Source
    if (-not $launcher) {
        $l = Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"
        if (Test-Path $l) { $launcher = $l }
    }
    if ($launcher) {
        $exe = & $launcher -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" }
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA "Programs\Python"), "C:\Program Files", "C:\" `
        -Filter python.exe -Depth 1 -ErrorAction SilentlyContinue | Where-Object { $_.DirectoryName -match "Python3\d+" } |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    foreach ($c in $candidates) {
        if (-not (Test-Path $c)) { continue }
        # Captured, or its output would become part of this function's result; checked by
        # what it prints, since any program exits 0 when it ignores the arguments.
        $said = & $c -c "import sys; print('phoneuse-python-ok' if sys.version_info >= (3, 11) else 'too-old')" 2>$null
        if ("$said".Trim() -eq "phoneuse-python-ok") { return $c }
    }
    throw "No Python 3.11 or newer found. Install it from https://www.python.org/downloads/ (or set PHONEUSE_PYTHON)."
}
