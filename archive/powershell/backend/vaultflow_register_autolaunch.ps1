# ============================================================
#  VAULTFLOW - register the SD-card auto-launch scheduled task
#  Run this ONCE, from an elevated (Run as Administrator) PowerShell.
#  It enables the device-connect event log and creates a Task
#  Scheduler task that fires vaultflow_autolaunch.ps1 whenever
#  Windows logs a new device starting (Event ID 2003 in
#  Microsoft-Windows-DriverFrameworks-UserMode/Operational).
# ============================================================

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Re-run this script as Administrator (right-click -> Run with PowerShell as admin)." -ForegroundColor Red
    exit 1
}

$ScriptDir     = Split-Path -Parent $MyInvocation.MyCommand.Path
$AutolaunchPs1 = Join-Path $ScriptDir "vaultflow_autolaunch.ps1"
$TaskName      = "VaultFlow SD Card Autolaunch"

wevtutil sl Microsoft-Windows-DriverFrameworks-UserMode/Operational /e:true
Write-Host "Enabled Microsoft-Windows-DriverFrameworks-UserMode/Operational event log." -ForegroundColor Green

$QueryXml = @"
<QueryList>
  <Query Id="0" Path="Microsoft-Windows-DriverFrameworks-UserMode/Operational">
    <Select Path="Microsoft-Windows-DriverFrameworks-UserMode/Operational">*[System[(EventID=2003)]]</Select>
  </Query>
</QueryList>
"@

$CimTriggerClass = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
$Trigger = New-CimInstance -CimClass $CimTriggerClass -ClientOnly -Property @{
    Subscription = $QueryXml
    Enabled      = $true
}

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$AutolaunchPs1`""

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -User $env:USERNAME `
    -RunLevel Limited `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "Insert an SD card to test - VaultFlow Ingest should pop up automatically." -ForegroundColor Green
