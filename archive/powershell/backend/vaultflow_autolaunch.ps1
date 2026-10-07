# ============================================================
#  VAULTFLOW AUTOLAUNCH
#  Fired by a Task Scheduler event trigger when Windows detects
#  a new device (see README "Auto-launch on SD card insert").
#  Checks whether the new drive looks like a camera SD card
#  (removable + has a DCIM folder) before popping up the GUI,
#  since the trigger event fires for USB devices in general.
# ============================================================

$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$IngestPath = Join-Path $ScriptDir "..\vaultflow_ingest.ps1"

function DetectSDCard {
    foreach ($drive in [System.IO.DriveInfo]::GetDrives()) {
        if ($drive.DriveType -eq "Removable" -and $drive.IsReady) {
            if (Test-Path (Join-Path $drive.RootDirectory.FullName "DCIM")) {
                return $drive.Name
            }
        }
    }
    return $null
}

if (-not (DetectSDCard)) { exit }

$AlreadyRunning = Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*vaultflow_ingest.ps1*" }

if ($AlreadyRunning) { exit }

Start-Process -FilePath "powershell.exe" `
    -ArgumentList @("-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", "`"$IngestPath`"") `
    -WindowStyle Hidden
