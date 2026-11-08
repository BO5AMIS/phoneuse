# Let the iPhone reach UxPlay (AirPlay) through Windows Firewall. Needs admin: install.ps1
# runs it elevated, which shows one UAC prompt.
#   powershell -ExecutionPolicy Bypass -File scripts\windows\firewall.ps1 -Program C:\msys64\ucrt64\bin\uxplay.exe
#
# One rule for the program (all its ports: AirPlay TCP 7000/7001/7100, UDP 6000/6001/7011,
# and mDNS UDP 5353 for its built-in responder), on every network profile, but only
# from the local subnet: the phone is on the same network, nobody else needs it.
# Rules Windows made for uxplay.exe when its own "Allow access?" prompt was answered
# or dismissed are removed first: a Block rule there would override this one.
param([Parameter(Mandatory = $true)][string]$Program, [switch]$Remove)
$ErrorActionPreference = "Stop"
$Program = (Resolve-Path $Program).Path

Get-NetFirewallApplicationFilter -Program $Program -ErrorAction SilentlyContinue |
    Get-NetFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if ($Remove) { Write-Host "removed the firewall rules for $Program"; exit 0 }

New-NetFirewallRule -DisplayName "PhoneUse: AirPlay receiver (UxPlay)" -Group "PhoneUse" `
    -Description "Lets the iPhone mirror its screen to phoneuse (AirPlay, mDNS) from the local network." `
    -Direction Inbound -Program $Program -Action Allow -Profile Any -RemoteAddress LocalSubnet | Out-Null
Write-Host "firewall: the local network may reach $Program" -ForegroundColor Green
