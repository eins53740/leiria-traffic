# Registers \BD\LeiriaTraffic\WebUI: serves the web UI on the LAN (port 8765) from logon on.
# Stop it by hand with:  Stop-ScheduledTask -TaskPath '\BD\LeiriaTraffic\' -TaskName WebUI
# The task needs no elevation; the firewall rule does, so run it once elevated as well.
# Idempotent: re-running replaces both.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent
$port = 8765

$action = New-ScheduledTaskAction -Execute "$repo\.venv\Scripts\pythonw.exe" `
    -Argument "-m leiria_traffic.cli serve --host 0.0.0.0 --port $port" -WorkingDirectory $repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:COMPUTERNAME\bsdias"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:COMPUTERNAME\bsdias" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskPath '\BD\LeiriaTraffic\' -TaskName WebUI -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force `
    -Description "leiria-traffic web UI on http://${env:COMPUTERNAME}:$port (LAN, access key in .env). Stop manually." | Out-Null

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { "registered task; firewall rule skipped (not elevated): re-run this script as administrator"; return }

# Private profile only: the plant (PLC/IND/ECS/DMZ) interfaces are Public and stay closed.
$rule = "leiria-traffic web UI (TCP-In $port)"
Get-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $rule -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow `
    -Profile Private | Out-Null
"registered task and firewall rule"
