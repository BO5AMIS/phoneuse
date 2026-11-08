# phoneuse on Windows: everything it needs, in one go. Safe to run again (it updates).
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 [-NoVision] [-NoUxPlay] [-NoFirewall] [-NoPath]
#
#   1. .venv with phoneuse and its Python packages (PyAV, aiohttp, the Windows Runtime
#      Bluetooth bindings, the MCP SDK); `phoneuse` and `phoneuse-mcp` launchers in bin\
#   2. MSYS2 (C:\msys64) and UxPlay, the AirPlay receiver, built from source
#   3. a Windows Firewall rule so the iPhone can reach UxPlay (one UAC prompt)
#   4. bin\ on your PATH
#   5. the screen-reading models (.venv-vision; a few GB; skip with -NoVision)
#   6. `phoneuse doctor`, which checks all of it
param([switch]$NoVision, [switch]$NoUxPlay, [switch]$NoFirewall, [switch]$NoPath)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
. "$PSScriptRoot\common.ps1"
$Msys = if ($env:PHONEUSE_MSYS2) { $env:PHONEUSE_MSYS2 } else { "C:\msys64" }
$UxPlay = Join-Path $Msys "ucrt64\bin\uxplay.exe"

# 1. Python
$Py = Find-Python
Write-Step "Python: $Py"
if (-not (Test-Path "$Root\.venv\Scripts\python.exe")) { & $Py -m venv "$Root\.venv" }
$VPy = "$Root\.venv\Scripts\python.exe"
& $VPy -m pip install --quiet --upgrade pip
& $VPy -m pip install --quiet -e $Root
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
foreach ($exe in "phoneuse.exe", "phoneuse-mcp.exe") {
    # pip's launchers point at .venv's python by absolute path, so they work from bin\.
    # A launcher that is running (`phoneuse install` itself, or `phoneuse up`) can't be
    # replaced; it only needs replacing when it changed.
    $src, $dst = "$Root\.venv\Scripts\$exe", "$Root\bin\$exe"
    if ((Test-Path $dst) -and (Get-FileHash $src).Hash -eq (Get-FileHash $dst).Hash) { continue }
    try { Copy-Item $src $dst -Force }
    catch { Write-Warning "couldn't update bin\$exe while it runs; stop phoneuse and run this again" }
}

# 2. UxPlay
function Invoke-Msys($script) {
    $env:MSYSTEM = "UCRT64"; $env:CHERE_INVOKING = "1"
    & "$Msys\usr\bin\bash.exe" -lc $script
}
if (-not $NoUxPlay) {
    if (-not (Test-Path "$Msys\usr\bin\bash.exe")) {
        Write-Step "Installing MSYS2 to $Msys (UxPlay's build tools and GStreamer)"
        $sfx = Join-Path $env:TEMP "msys2-base.sfx.exe"
        Invoke-WebRequest "https://repo.msys2.org/distrib/msys2-x86_64-latest.sfx.exe" -OutFile $sfx
        Start-Process -FilePath $sfx -ArgumentList "-y", "-o$(Split-Path -Parent $Msys)\" -Wait -NoNewWindow
        Invoke-Msys "pacman-key --init; pacman-key --populate msys2" | Out-Null
        # The first update replaces MSYS2's own core and ends the shell; the second finishes.
        Invoke-Msys "pacman -Syuu --noconfirm --disable-download-timeout" | Out-Null
    }
    Invoke-Msys "pacman -Syuu --noconfirm --disable-download-timeout" | Out-Null
    Write-Step "Building UxPlay"
    Push-Location $Root
    try { Invoke-Msys "bash scripts/windows/build-uxplay.sh" } finally { Pop-Location }
    if (-not (Test-Path $UxPlay)) { throw "UxPlay didn't build; see the output above" }
}

# 3. Firewall
if (-not $NoFirewall -and (Test-Path $UxPlay)) {
    Write-Step "Firewall rule for UxPlay (Windows asks for admin rights once)"
    $fwArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\firewall.ps1`" -Program `"$UxPlay`""
    try {
        $p = Start-Process powershell -Verb RunAs -ArgumentList $fwArgs -Wait -PassThru
        if ($p.ExitCode -ne 0) { Write-Warning "the firewall rule wasn't added (exit $($p.ExitCode))" }
    } catch {
        Write-Warning "the firewall rule wasn't added ($($_.Exception.Message)). Run as admin: powershell -File scripts\windows\firewall.ps1 -Program $UxPlay"
    }
}

# 4. PATH
if (-not $NoPath) {
    $bin = Join-Path $Root "bin"
    $user = [string][Environment]::GetEnvironmentVariable("Path", "User")  # $null when unset
    if (($user -split ";") -notcontains $bin) {
        [Environment]::SetEnvironmentVariable("Path", (($user.TrimEnd(";") + ";" + $bin).TrimStart(";")), "User")
        Write-Step "Added $bin to your PATH (new terminals see it)"
    }
}

# 5. Vision models
if (-not $NoVision) { & "$PSScriptRoot\setup-vision.ps1" }

# 6. Check
Write-Step "Checking the setup"
& "$Root\bin\phoneuse.exe" doctor
