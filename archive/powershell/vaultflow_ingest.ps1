# ============================================================
#  VAULTFLOW INGEST (Windows)
#  Camera-agnostic SD card ingest: sort by EXIF date, dedupe,
#  verify, ready to format. Configure your RAW/video/aux file
#  extensions in the FILE TYPES section to match your camera.
# ============================================================

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# -- CONFIG FILE ---------------------------------------------
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ScriptDir -or $ScriptDir -eq "") {
    $ScriptDir = Get-Location
}
# Archived location: config and assets live two levels up, in the repo root,
# shared with the Python version.
$RepoRoot   = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$ConfigFile = Join-Path $RepoRoot "vaultflow_config.json"
$IconFile   = Join-Path $RepoRoot "assets\vaultflow.ico"
$LogoFile   = Join-Path $RepoRoot "assets\vaultflow-icon.png"

function LoadConfig {
    if (Test-Path $ConfigFile) {
        try {
            $raw = Get-Content $ConfigFile -Raw | ConvertFrom-Json
            return @{
                Dest           = $raw.Dest
                LogDir         = $raw.LogDir
                Exiftool       = $raw.Exiftool
                RawExt         = if ($raw.RawExt)   { $raw.RawExt }   else { "nef" }
                VideoExt       = if ($raw.VideoExt) { $raw.VideoExt } else { "mp4" }
                AudioExt       = if ($raw.AudioExt) { $raw.AudioExt } else { "wav" }
                AuxExt         = $raw.AuxExt
                Copyright      = $raw.Copyright
            }
        } catch {}
    }
    return @{
        Dest           = "$env:USERPROFILE\Pictures\PhotoVault"
        LogDir         = "$env:USERPROFILE\Pictures\PhotoVault\_logs"
        Exiftool       = "C:\exiftool\exiftool.exe"
        RawExt         = "nef"
        VideoExt       = "mp4"
        AudioExt       = "wav"
        AuxExt         = ""
        Copyright      = ""
    }
}

function SaveConfig($dest, $logDir, $exiftool, $rawExt, $videoExt, $audioExt, $auxExt, $copyright) {
    @{
        Dest           = $dest
        LogDir         = $logDir
        Exiftool       = $exiftool
        RawExt         = $rawExt
        VideoExt       = $videoExt
        AudioExt       = $audioExt
        AuxExt         = $auxExt
        Copyright      = $copyright
    } | ConvertTo-Json | Set-Content $ConfigFile -Encoding utf8
}

# Parses a comma-separated extension list ("nef, cr2 , .arw") into a clean
# lowercase array without dots ("nef","cr2","arw"), so users can paste
# whatever format they're used to.
function ParseExtList($raw) {
    if (-not $raw) { return @() }
    return @($raw -split "," | ForEach-Object { $_.Trim().TrimStart(".").ToLower() } | Where-Object { $_ -ne "" })
}

function GetFilesByExt($path, $extList) {
    if ($extList.Count -eq 0) { return @() }
    return @(Get-ChildItem -Path $path -Recurse -Include ($extList | ForEach-Object { "*.$_" }) -ErrorAction SilentlyContinue)
}

# -- CLIENT FOLDER SCAFFOLD & JOB-TYPE METADATA PRESETS -------
# Every shoot folder gets these three: RAW holds the ingested files, PX01/LR01
# are left empty as numbered export targets (Photoshop/Lightroom rounds).
$ClientFolderTemplate = @(
    "RAW", "PX01", "LR01"
)

# RAW is split by media type so ingested files land pre-sorted instead of
# all mixed together in one folder. CAM_RAW (not "RAW") holds the camera raw
# files themselves, since a folder literally named "RAW" would sit directly
# under a parent already named "RAW". MISC (not "AUX") holds proxy/360
# footage, since "AUX" is a reserved Windows device name and New-Item can't
# create a real folder with that name.
$RawTypeFolders = @("CAM_RAW", "JPG", "VIDEO", "AUDIO", "MISC")


$Config = LoadConfig

# -- COLOURS -------------------------------------------------
$BG       = [System.Drawing.Color]::FromArgb(13,  17,  23)
$PANEL    = [System.Drawing.Color]::FromArgb(22,  30,  40)
$TEAL     = [System.Drawing.Color]::FromArgb(0,   210, 190)
$TEAL_DIM = [System.Drawing.Color]::FromArgb(0,   140, 126)
$FG       = [System.Drawing.Color]::FromArgb(220, 230, 240)
$FG_DIM   = [System.Drawing.Color]::FromArgb(100, 130, 150)
$RED      = [System.Drawing.Color]::FromArgb(255, 85,  85)
$GREEN    = [System.Drawing.Color]::FromArgb(80,  220, 140)
$YELLOW   = [System.Drawing.Color]::FromArgb(255, 200, 60)
$ORANGE   = [System.Drawing.Color]::FromArgb(255, 140, 40)
$FONT_UI  = New-Object System.Drawing.Font("Segoe UI", 9)
$FONT_LBL = New-Object System.Drawing.Font("Segoe UI Semibold", 8.5)
$FONT_TTL = New-Object System.Drawing.Font("Segoe UI Light", 18)
$FONT_SUB = New-Object System.Drawing.Font("Segoe UI", 8)
$FONT_LOG = New-Object System.Drawing.Font("Consolas", 8.5)

# -- FORM ----------------------------------------------------
$Form = New-Object System.Windows.Forms.Form
$Form.Text            = "VaultFlow Ingest"
$Form.Size            = New-Object System.Drawing.Size(1140, 992)
$Form.StartPosition   = "CenterScreen"
$Form.BackColor       = $BG
$Form.ForeColor       = $FG
$Form.Font            = $FONT_UI
$Form.FormBorderStyle = "FixedSingle"
$Form.MaximizeBox     = $false
$Form.ShowInTaskbar   = $true
if (Test-Path $IconFile) {
    try { $Form.Icon = New-Object System.Drawing.Icon($IconFile) } catch {}
}

# -- HELPERS -------------------------------------------------
function MakeLabel($text, $x, $y, $w = 300) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text      = $text
    $l.Font      = $FONT_LBL
    $l.ForeColor = $FG_DIM
    $l.Location  = New-Object System.Drawing.Point($x, $y)
    $l.Size      = New-Object System.Drawing.Size($w, 18)
    return $l
}

function MakeTextBox($x, $y, $w, $val) {
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location    = New-Object System.Drawing.Point($x, $y)
    $t.Size        = New-Object System.Drawing.Size($w, 26)
    $t.BackColor   = $PANEL
    $t.ForeColor   = $FG
    $t.BorderStyle = "FixedSingle"
    $t.Font        = $FONT_UI
    $t.Text        = $val
    return $t
}

function MakePlaceholderTextBox($x, $y, $w, $placeholder) {
    $t = New-Object System.Windows.Forms.TextBox
    $t.Location    = New-Object System.Drawing.Point($x, $y)
    $t.Size        = New-Object System.Drawing.Size($w, 26)
    $t.BackColor   = $PANEL
    $t.ForeColor   = $FG_DIM
    $t.BorderStyle = "FixedSingle"
    $t.Font        = $FONT_UI
    $t.Text        = $placeholder
    $t.Tag         = $placeholder
    $t.Add_GotFocus({
        if ($this.Text -eq $this.Tag) { $this.Text = ""; $this.ForeColor = $FG }
    })
    $t.Add_LostFocus({
        if ($this.Text -eq "") { $this.Text = $this.Tag; $this.ForeColor = $FG_DIM }
    })
    return $t
}

function MakeBrowseBox($x, $y, $w, $val, $isFile) {
    $t = MakeTextBox $x $y ($w - 80) $val
    $btn = New-Object System.Windows.Forms.Button
    $btn.Text      = "Browse"
    $btn.Location  = New-Object System.Drawing.Point(($x + $w - 76), ($y - 1))
    $btn.Size      = New-Object System.Drawing.Size(76, 28)
    $btn.BackColor = $PANEL
    $btn.ForeColor = $FG_DIM
    $btn.FlatStyle = "Flat"
    $btn.Font      = $FONT_SUB
    $btn.FlatAppearance.BorderColor = $FG_DIM
    $btn.FlatAppearance.BorderSize  = 1
    $btn.Cursor    = "Hand"
    # Store both isFile flag and textbox reference in Tag so the closure can access them via $this
    $btn.Tag       = @{ IsFile = $isFile; TextBox = $t }
    $btn.Add_Click({
        $isFilePicker = $this.Tag.IsFile
        $txtBox       = $this.Tag.TextBox
        if ($isFilePicker) {
            $dlg = New-Object System.Windows.Forms.OpenFileDialog
            $dlg.Title  = "Select exiftool.exe"
            $dlg.Filter = "Executables (*.exe)|*.exe"
            if ($txtBox.Text -ne "" -and (Test-Path (Split-Path $txtBox.Text))) { $dlg.InitialDirectory = Split-Path $txtBox.Text }
            if ($dlg.ShowDialog() -eq "OK") { $txtBox.Text = $dlg.FileName }
        } else {
            $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
            $dlg.Description = "Select folder"
            if ($txtBox.Text -ne "" -and (Test-Path $txtBox.Text)) { $dlg.SelectedPath = $txtBox.Text }
            if ($dlg.ShowDialog() -eq "OK") { $txtBox.Text = $dlg.SelectedPath }
        }
    })
    return @{ TextBox = $t; Button = $btn }
}

function AddDivider($y) {
    $d = New-Object System.Windows.Forms.Panel
    $d.BackColor = $PANEL
    $d.Location  = New-Object System.Drawing.Point(24, $y)
    $d.Size      = New-Object System.Drawing.Size(628, 1)
    $Form.Controls.Add($d)
}

function AddSectionHeader($text, $y) {
    $l = New-Object System.Windows.Forms.Label
    $l.Text      = $text
    $l.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 7.5)
    $l.ForeColor = $TEAL
    $l.BackColor = [System.Drawing.Color]::FromArgb(0, 30, 28)
    $l.Location  = New-Object System.Drawing.Point(24, $y)
    $l.Size      = New-Object System.Drawing.Size(628, 20)
    $l.TextAlign = "MiddleLeft"
    $l.Padding   = New-Object System.Windows.Forms.Padding(6, 0, 0, 0)
    $Form.Controls.Add($l)
}

function FormatBytes($bytes) {
    if ($bytes -ge 1GB) { return "{0:N2} GB" -f ($bytes / 1GB) }
    if ($bytes -ge 1MB) { return "{0:N1} MB" -f ($bytes / 1MB) }
    if ($bytes -ge 1KB) { return "{0:N0} KB" -f ($bytes / 1KB) }
    return "$bytes B"
}

function FormatSpeed($bps) {
    if ($bps -ge 1MB) { return "{0:N1} MB/s" -f ($bps / 1MB) }
    if ($bps -ge 1KB) { return "{0:N0} KB/s" -f ($bps / 1KB) }
    return "$([int]$bps) B/s"
}

function FormatETA($secs) {
    if ($secs -le 0) { return "--:--" }
    $ts = [TimeSpan]::FromSeconds($secs)
    if ($ts.TotalHours -ge 1)   { return "{0}h {1:D2}m" -f [int]$ts.TotalHours, $ts.Minutes }
    if ($ts.TotalMinutes -ge 1) { return "{0}m {1:D2}s" -f $ts.Minutes, $ts.Seconds }
    return "{0}s" -f $ts.Seconds
}

# ============================================================
#  TITLE
# ============================================================
if (Test-Path $LogoFile) {
    $LogoBox = New-Object System.Windows.Forms.PictureBox
    $LogoBox.Image       = [System.Drawing.Image]::FromFile($LogoFile)
    $LogoBox.SizeMode    = "Zoom"
    $LogoBox.Location    = New-Object System.Drawing.Point(24, 14)
    $LogoBox.Size        = New-Object System.Drawing.Size(44, 44)
    $LogoBox.BackColor   = [System.Drawing.Color]::Transparent
    $Form.Controls.Add($LogoBox)
    $TitleX = 76
} else {
    $TitleX = 24
}

$LblTitle = New-Object System.Windows.Forms.Label
$LblTitle.Text      = "VAULTFLOW"
$LblTitle.Font      = $FONT_TTL
$LblTitle.ForeColor = $TEAL
$LblTitle.Location  = New-Object System.Drawing.Point($TitleX, 18)
$LblTitle.Size      = New-Object System.Drawing.Size(200, 36)
$Form.Controls.Add($LblTitle)

$LblSub = New-Object System.Windows.Forms.Label
$LblSub.Text      = "SD CARD INGEST UTILITY"
$LblSub.Font      = $FONT_SUB
$LblSub.ForeColor = $FG_DIM
$LblSub.Location  = New-Object System.Drawing.Point(($TitleX + 2), 52)
$LblSub.Size      = New-Object System.Drawing.Size(200, 18)
$Form.Controls.Add($LblSub)

$TitleLine = New-Object System.Windows.Forms.Panel
$TitleLine.BackColor = $TEAL
$TitleLine.Location  = New-Object System.Drawing.Point(24, 76)
$TitleLine.Size      = New-Object System.Drawing.Size(1092, 1)
$Form.Controls.Add($TitleLine)

# ============================================================
#  SECTION: PATHS
# ============================================================
AddSectionHeader "  PATHS" 86

$Form.Controls.Add((MakeLabel "VAULT DESTINATION" 24 116))
$DestBox = MakeBrowseBox 24 134 628 $Config.Dest $false
$Form.Controls.Add($DestBox.TextBox)
$Form.Controls.Add($DestBox.Button)

$Form.Controls.Add((MakeLabel "LOG FOLDER" 24 170))
$LogBox = MakeBrowseBox 24 188 628 $Config.LogDir $false
$Form.Controls.Add($LogBox.TextBox)
$Form.Controls.Add($LogBox.Button)

$Form.Controls.Add((MakeLabel "EXIFTOOL PATH" 24 224))
$ExifBox = MakeBrowseBox 24 242 628 $Config.Exiftool $true
$Form.Controls.Add($ExifBox.TextBox)
$Form.Controls.Add($ExifBox.Button)

$DestBox.TextBox.Add_TextChanged({
    $newDest = $DestBox.TextBox.Text.Trim()
    if ($newDest -ne "") { $LogBox.TextBox.Text = Join-Path $newDest "_logs" }
})

$BtnSaveConfig = New-Object System.Windows.Forms.Button
$BtnSaveConfig.Text      = "Save Paths"
$BtnSaveConfig.Location  = New-Object System.Drawing.Point(24, 278)
$BtnSaveConfig.Size      = New-Object System.Drawing.Size(100, 26)
$BtnSaveConfig.BackColor = $PANEL
$BtnSaveConfig.ForeColor = $TEAL
$BtnSaveConfig.FlatStyle = "Flat"
$BtnSaveConfig.Font      = $FONT_SUB
$BtnSaveConfig.FlatAppearance.BorderColor = $TEAL
$BtnSaveConfig.FlatAppearance.BorderSize  = 1
$BtnSaveConfig.Cursor    = "Hand"
$Form.Controls.Add($BtnSaveConfig)

$LblSaveStatus = New-Object System.Windows.Forms.Label
$LblSaveStatus.Text      = ""
$LblSaveStatus.Font      = $FONT_SUB
$LblSaveStatus.ForeColor = $GREEN
$LblSaveStatus.Location  = New-Object System.Drawing.Point(134, 283)
$LblSaveStatus.Size      = New-Object System.Drawing.Size(300, 18)
$Form.Controls.Add($LblSaveStatus)

$BtnSaveConfig.Add_Click({
    $auxVal      = if ($TxtAuxExt.Text -eq $TxtAuxExt.Tag) { "" } else { $TxtAuxExt.Text.Trim() }
    $copyVal     = if ($TxtCopyright.Text -eq $TxtCopyright.Tag) { "" } else { $TxtCopyright.Text.Trim() }
    SaveConfig $DestBox.TextBox.Text.Trim() $LogBox.TextBox.Text.Trim() $ExifBox.TextBox.Text.Trim() $TxtRawExt.Text.Trim() $TxtVideoExt.Text.Trim() $TxtAudioExt.Text.Trim() $auxVal $copyVal
    $LblSaveStatus.Text = "Saved."
    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 2000
    $timer.Add_Tick({ $LblSaveStatus.Text = ""; $this.Stop() })
    $timer.Start()
})

AddDivider 314

# ============================================================
#  SECTION: FILE TYPES  (camera-agnostic - configure per camera)
# ============================================================
AddSectionHeader "  FILE TYPES" 324

$Form.Controls.Add((MakeLabel "RAW EXTENSIONS" 24 354 148))
$DefaultRaw = if ($Config.RawExt -and $Config.RawExt -ne "nef") { $Config.RawExt } else { "nef, cr2, cr3, arw, raf, orf, rw2, dng" }
$TxtRawExt = MakeTextBox 24 372 148 $DefaultRaw
$Form.Controls.Add($TxtRawExt)

$Form.Controls.Add((MakeLabel "VIDEO EXTENSIONS" 184 354 148))
$TxtVideoExt = MakeTextBox 184 372 148 $Config.VideoExt
$Form.Controls.Add($TxtVideoExt)

$Form.Controls.Add((MakeLabel "AUDIO EXTENSIONS" 344 354 148))
$TxtAudioExt = MakeTextBox 344 372 148 $Config.AudioExt
$Form.Controls.Add($TxtAudioExt)

$Form.Controls.Add((MakeLabel "MISC EXT. (optional)" 504 354 148))
$TxtAuxExt = MakePlaceholderTextBox 504 372 148 "e.g. lrv, insv"
if ($Config.AuxExt) { $TxtAuxExt.Text = $Config.AuxExt; $TxtAuxExt.ForeColor = $FG }
$Form.Controls.Add($TxtAuxExt)

$LblFileTypesHint = New-Object System.Windows.Forms.Label
$LblFileTypesHint.Text      = "Comma-separated, no dots. RAW example: nef, cr2, cr3, arw, raf, orf, rw2, dng.  AUDIO example: wav, mp3.  MISC = proxy/360 footage, e.g. GoPro/Insta360 .lrv/.insv"
$LblFileTypesHint.Font      = $FONT_SUB
$LblFileTypesHint.ForeColor = $FG_DIM
$LblFileTypesHint.Location  = New-Object System.Drawing.Point(24, 402)
$LblFileTypesHint.Size      = New-Object System.Drawing.Size(628, 18)
$Form.Controls.Add($LblFileTypesHint)

AddDivider 434

# ============================================================
#  SECTION: METADATA  (embedded into every ingested file via ExifTool)
# ============================================================
AddSectionHeader "  METADATA  /  NOTIFICATIONS" 444

$Form.Controls.Add((MakeLabel "COPYRIGHT" 24 474 370))
$TxtCopyright = MakePlaceholderTextBox 24 492 370 "(c) 2026 Jane Doe"
if ($Config.Copyright) { $TxtCopyright.Text = $Config.Copyright; $TxtCopyright.ForeColor = $FG }
$Form.Controls.Add($TxtCopyright)

$LblMetadataHint = New-Object System.Windows.Forms.Label
$LblMetadataHint.Text      = "Copyright is written to ingested files via ExifTool (auto-detected from the card on dry run if blank)."
$LblMetadataHint.Font      = $FONT_SUB
$LblMetadataHint.ForeColor = $FG_DIM
$LblMetadataHint.Location  = New-Object System.Drawing.Point(24, 522)
$LblMetadataHint.Size      = New-Object System.Drawing.Size(628, 18)
$Form.Controls.Add($LblMetadataHint)

AddDivider 554

# ============================================================
#  SECTION: SESSION
# ============================================================
AddSectionHeader "  SESSION" 564

$Form.Controls.Add((MakeLabel "SD CARD DRIVE" 24 594))
$CmbDrive = New-Object System.Windows.Forms.ComboBox
$CmbDrive.Location      = New-Object System.Drawing.Point(24, 612)
$CmbDrive.Size          = New-Object System.Drawing.Size(100, 26)
$CmbDrive.BackColor     = $PANEL
$CmbDrive.ForeColor     = $FG
$CmbDrive.FlatStyle     = "Flat"
$CmbDrive.Font          = $FONT_UI
$CmbDrive.DropDownStyle = "DropDownList"
foreach ($Letter in @("C","D","E","F","G","H","I")) { $CmbDrive.Items.Add($Letter + ":\") | Out-Null }
$CmbDrive.SelectedItem = "D:\"
$Form.Controls.Add($CmbDrive)

$LblAutoDetect = New-Object System.Windows.Forms.Label
$LblAutoDetect.Text      = "Scanning drives..."
$LblAutoDetect.Font      = $FONT_SUB
$LblAutoDetect.ForeColor = $FG_DIM
$LblAutoDetect.Location  = New-Object System.Drawing.Point(136, 616)
$LblAutoDetect.Size      = New-Object System.Drawing.Size(300, 18)
$Form.Controls.Add($LblAutoDetect)

# -- EJECT TOGGLE (inlined) ----------------------------------
$script:EjectChecked = $false

$EjectTrack = New-Object System.Windows.Forms.Panel
$EjectTrack.Location  = New-Object System.Drawing.Point(24, 648)
$EjectTrack.Size      = New-Object System.Drawing.Size(36, 18)
$EjectTrack.BackColor = $PANEL
$EjectTrack.Cursor    = "Hand"

$EjectThumb = New-Object System.Windows.Forms.Panel
$EjectThumb.Size      = New-Object System.Drawing.Size(12, 12)
$EjectThumb.Location  = New-Object System.Drawing.Point(3, 3)
$EjectThumb.BackColor = $FG_DIM
$EjectThumb.Cursor    = "Hand"
$EjectTrack.Controls.Add($EjectThumb)

$EjectLabel = New-Object System.Windows.Forms.Label
$EjectLabel.Text      = "Eject SD card after ingest"
$EjectLabel.Font      = $FONT_SUB
$EjectLabel.ForeColor = $FG_DIM
$EjectLabel.Location  = New-Object System.Drawing.Point(68, 649)
$EjectLabel.Size      = New-Object System.Drawing.Size(300, 18)
$EjectLabel.Cursor    = "Hand"

$Form.Controls.Add($EjectTrack)
$Form.Controls.Add($EjectLabel)

$EjectToggle = {
    $script:EjectChecked = -not $script:EjectChecked
    if ($script:EjectChecked) {
        $EjectTrack.BackColor = $TEAL
        $EjectThumb.BackColor = $BG
        $EjectThumb.Location  = New-Object System.Drawing.Point(21, 3)
        $EjectLabel.ForeColor = $FG
    } else {
        $EjectTrack.BackColor = $PANEL
        $EjectThumb.BackColor = $FG_DIM
        $EjectThumb.Location  = New-Object System.Drawing.Point(3, 3)
        $EjectLabel.ForeColor = $FG_DIM
    }
}
$EjectTrack.Add_Click($EjectToggle)
$EjectThumb.Add_Click($EjectToggle)
$EjectLabel.Add_Click($EjectToggle)

# ============================================================
#  SECTION: PREVIEW  (thumbnail filmstrip of files queued for copy)
# ============================================================
AddSectionHeader "  PREVIEW  (files queued for copy)" 682

$ThumbnailStrip = New-Object System.Windows.Forms.FlowLayoutPanel
$ThumbnailStrip.Location      = New-Object System.Drawing.Point(24, 704)
$ThumbnailStrip.Size          = New-Object System.Drawing.Size(628, 100)
$ThumbnailStrip.BackColor     = $PANEL
$ThumbnailStrip.FlowDirection = "LeftToRight"
$ThumbnailStrip.WrapContents  = $false
$ThumbnailStrip.AutoScroll    = $true
$Form.Controls.Add($ThumbnailStrip)

$LblPreviewHint = New-Object System.Windows.Forms.Label
$LblPreviewHint.Text      = "Populated when a dry run or live ingest starts."
$LblPreviewHint.Font      = $FONT_SUB
$LblPreviewHint.ForeColor = $FG_DIM
$LblPreviewHint.Location  = New-Object System.Drawing.Point(10, 10)
$LblPreviewHint.Size      = New-Object System.Drawing.Size(400, 18)
$ThumbnailStrip.Controls.Add($LblPreviewHint)

$LblFolderStructure = New-Object System.Windows.Forms.Label
$LblFolderStructure.Text      = "Files are automatically organized into: CAM_RAW / JPG / VIDEO / AUDIO / MISC subfolders"
$LblFolderStructure.Font      = $FONT_SUB
$LblFolderStructure.ForeColor = $TEAL
$LblFolderStructure.Location  = New-Object System.Drawing.Point(24, 806)
$LblFolderStructure.Size      = New-Object System.Drawing.Size(628, 18)
$Form.Controls.Add($LblFolderStructure)

# Dry run banner - shown while a dry run is in progress
$DryRunBanner = New-Object System.Windows.Forms.Panel
$DryRunBanner.BackColor = [System.Drawing.Color]::FromArgb(60, 50, 0)
$DryRunBanner.Location  = New-Object System.Drawing.Point(24, 828)
$DryRunBanner.Size      = New-Object System.Drawing.Size(628, 24)
$DryRunBanner.Visible   = $false
$LblDryRunBanner = New-Object System.Windows.Forms.Label
$LblDryRunBanner.Text      = "  DRY RUN ACTIVE - No files will be copied or moved"
$LblDryRunBanner.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 8.5)
$LblDryRunBanner.ForeColor = $YELLOW
$LblDryRunBanner.BackColor = [System.Drawing.Color]::Transparent
$LblDryRunBanner.Location  = New-Object System.Drawing.Point(0, 4)
$LblDryRunBanner.Size      = New-Object System.Drawing.Size(628, 18)
$DryRunBanner.Controls.Add($LblDryRunBanner)
$Form.Controls.Add($DryRunBanner)

AddDivider 852

# ============================================================
#  BUTTONS
# ============================================================
$GOLD = [System.Drawing.Color]::FromArgb(212, 175, 55)

$BtnDryRun = New-Object System.Windows.Forms.Button
$BtnDryRun.Text      = "START DRY RUN"
$BtnDryRun.Location  = New-Object System.Drawing.Point(24, 862)
$BtnDryRun.Size      = New-Object System.Drawing.Size(170, 34)
$BtnDryRun.BackColor = $PANEL
$BtnDryRun.ForeColor = $FG
$BtnDryRun.FlatStyle = "Flat"
$BtnDryRun.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9.5)
$BtnDryRun.FlatAppearance.BorderColor = $FG_DIM
$BtnDryRun.FlatAppearance.BorderSize  = 1
$BtnDryRun.Cursor    = "Hand"
$Form.Controls.Add($BtnDryRun)

$BtnLiveIngest = New-Object System.Windows.Forms.Button
$BtnLiveIngest.Text      = "START LIVE INGEST"
$BtnLiveIngest.Location  = New-Object System.Drawing.Point(204, 862)
$BtnLiveIngest.Size      = New-Object System.Drawing.Size(170, 34)
$BtnLiveIngest.BackColor = $TEAL
$BtnLiveIngest.ForeColor = $BG
$BtnLiveIngest.FlatStyle = "Flat"
$BtnLiveIngest.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9.5)
$BtnLiveIngest.FlatAppearance.BorderColor = $GOLD
$BtnLiveIngest.FlatAppearance.BorderSize  = 2
$BtnLiveIngest.Cursor    = "Hand"
$Form.Controls.Add($BtnLiveIngest)

# FlatStyle buttons ignore the system's built-in disabled look, so grey them
# out manually whenever Enabled changes - otherwise a "disabled" Live Ingest
# button still looks fully clickable (teal/gold) and misleads the user.
function SetLiveIngestEnabled($enabled) {
    $BtnLiveIngest.Enabled = $enabled
    if ($enabled) {
        $BtnLiveIngest.BackColor = $TEAL
        $BtnLiveIngest.ForeColor = $BG
        $BtnLiveIngest.FlatAppearance.BorderColor = $GOLD
        $BtnLiveIngest.Cursor    = "Hand"
    } else {
        $BtnLiveIngest.BackColor = $PANEL
        $BtnLiveIngest.ForeColor = $FG_DIM
        $BtnLiveIngest.FlatAppearance.BorderColor = $FG_DIM
        $BtnLiveIngest.Cursor    = "No"
    }
}

$BtnStop = New-Object System.Windows.Forms.Button
$BtnStop.Text      = "STOP"
$BtnStop.Location  = New-Object System.Drawing.Point(384, 862)
$BtnStop.Size      = New-Object System.Drawing.Size(80, 34)
$BtnStop.BackColor = $RED
$BtnStop.ForeColor = $FG
$BtnStop.FlatStyle = "Flat"
$BtnStop.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9.5)
$BtnStop.FlatAppearance.BorderSize = 0
$BtnStop.Cursor    = "Hand"
$BtnStop.Enabled   = $false
$Form.Controls.Add($BtnStop)

$LblStatus = New-Object System.Windows.Forms.Label
$LblStatus.Text      = "Ready."
$LblStatus.Font      = $FONT_SUB
$LblStatus.ForeColor = $FG_DIM
$LblStatus.Location  = New-Object System.Drawing.Point(484, 874)
$LblStatus.Size      = New-Object System.Drawing.Size(168, 18)
$Form.Controls.Add($LblStatus)

$BtnOpenFolder = New-Object System.Windows.Forms.Button
$BtnOpenFolder.Text      = "OPEN VAULT FOLDER"
$BtnOpenFolder.Location  = New-Object System.Drawing.Point(24, 904)
$BtnOpenFolder.Size      = New-Object System.Drawing.Size(308, 32)
$BtnOpenFolder.BackColor = $PANEL
$BtnOpenFolder.ForeColor = $FG_DIM
$BtnOpenFolder.FlatStyle = "Flat"
$BtnOpenFolder.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$BtnOpenFolder.FlatAppearance.BorderColor = $FG_DIM
$BtnOpenFolder.FlatAppearance.BorderSize  = 1
$BtnOpenFolder.Cursor    = "No"
$BtnOpenFolder.Enabled   = $false
$Form.Controls.Add($BtnOpenFolder)
$BtnOpenFolder.Add_Click({
    if ($script:LastIngestFolder -and (Test-Path $script:LastIngestFolder)) {
        Start-Process "explorer.exe" -ArgumentList "`"$($script:LastIngestFolder)`""
    }
})

# FlatStyle buttons don't auto-grey on Enabled = $false (same issue as Live Ingest).
function SetOpenFolderEnabled($enabled) {
    $BtnOpenFolder.Enabled = $enabled
    if ($enabled) {
        $BtnOpenFolder.ForeColor = $TEAL
        $BtnOpenFolder.FlatAppearance.BorderColor = $TEAL
        $BtnOpenFolder.Cursor   = "Hand"
    } else {
        $BtnOpenFolder.ForeColor = $FG_DIM
        $BtnOpenFolder.FlatAppearance.BorderColor = $FG_DIM
        $BtnOpenFolder.Cursor   = "No"
    }
}

$BtnVerify = New-Object System.Windows.Forms.Button
$BtnVerify.Text      = "VERIFY CARD vs VAULT"
$BtnVerify.Location  = New-Object System.Drawing.Point(344, 904)
$BtnVerify.Size      = New-Object System.Drawing.Size(308, 32)
$BtnVerify.BackColor = $PANEL
$BtnVerify.ForeColor = $GOLD
$BtnVerify.FlatStyle = "Flat"
$BtnVerify.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$BtnVerify.FlatAppearance.BorderColor = $GOLD
$BtnVerify.FlatAppearance.BorderSize  = 1
$BtnVerify.Cursor    = "Hand"
$Form.Controls.Add($BtnVerify)

# Same manual-greying as the other FlatStyle buttons.
function SetVerifyEnabled($enabled) {
    $BtnVerify.Enabled = $enabled
    if ($enabled) {
        $BtnVerify.ForeColor = $GOLD
        $BtnVerify.FlatAppearance.BorderColor = $GOLD
        $BtnVerify.Cursor    = "Hand"
    } else {
        $BtnVerify.ForeColor = $FG_DIM
        $BtnVerify.FlatAppearance.BorderColor = $FG_DIM
        $BtnVerify.Cursor    = "No"
    }
}

AddDivider 947

# ============================================================
#  PROGRESS
# ============================================================
$ProgressBar = New-Object System.Windows.Forms.ProgressBar
$ProgressBar.Location  = New-Object System.Drawing.Point(24, 957)
$ProgressBar.Size      = New-Object System.Drawing.Size(628, 14)
$ProgressBar.Minimum   = 0
$ProgressBar.Maximum   = 100
$ProgressBar.Value     = 0
$ProgressBar.Style     = "Continuous"
$ProgressBar.ForeColor = $TEAL
$ProgressBar.BackColor = $PANEL
$Form.Controls.Add($ProgressBar)

$LblPct = New-Object System.Windows.Forms.Label
$LblPct.Text      = "0%"
$LblPct.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
$LblPct.ForeColor = $TEAL
$LblPct.Location  = New-Object System.Drawing.Point(24, 978)
$LblPct.Size      = New-Object System.Drawing.Size(60, 18)
$Form.Controls.Add($LblPct)

$LblFiles = New-Object System.Windows.Forms.Label
$LblFiles.Text      = "0 / 0 files"
$LblFiles.Font      = $FONT_SUB
$LblFiles.ForeColor = $FG_DIM
$LblFiles.Location  = New-Object System.Drawing.Point(90, 978)
$LblFiles.Size      = New-Object System.Drawing.Size(160, 18)
$Form.Controls.Add($LblFiles)

$LblSpeed = New-Object System.Windows.Forms.Label
$LblSpeed.Text      = ""
$LblSpeed.Font      = $FONT_SUB
$LblSpeed.ForeColor = $FG_DIM
$LblSpeed.Location  = New-Object System.Drawing.Point(280, 978)
$LblSpeed.Size      = New-Object System.Drawing.Size(120, 18)
$Form.Controls.Add($LblSpeed)

$LblETA = New-Object System.Windows.Forms.Label
$LblETA.Text      = ""
$LblETA.Font      = $FONT_SUB
$LblETA.ForeColor = $FG_DIM
$LblETA.Location  = New-Object System.Drawing.Point(430, 978)
$LblETA.Size      = New-Object System.Drawing.Size(222, 18)
$Form.Controls.Add($LblETA)

$LblCurrentFile = New-Object System.Windows.Forms.Label
$LblCurrentFile.Text      = ""
$LblCurrentFile.Font      = $FONT_SUB
$LblCurrentFile.ForeColor = $FG_DIM
$LblCurrentFile.Location  = New-Object System.Drawing.Point(24, 998)
$LblCurrentFile.Size      = New-Object System.Drawing.Size(628, 18)
$Form.Controls.Add($LblCurrentFile)

# ============================================================
#  RIGHT PANEL - OUTPUT LOG (full height)
# ============================================================

# Vertical divider between left controls and right log
$VDivider = New-Object System.Windows.Forms.Panel
$VDivider.BackColor = $PANEL
$VDivider.Location  = New-Object System.Drawing.Point(668, 86)
$VDivider.Size      = New-Object System.Drawing.Size(1, 906)
$Form.Controls.Add($VDivider)

$LblLogHeader = New-Object System.Windows.Forms.Label
$LblLogHeader.Text      = "  OUTPUT LOG"
$LblLogHeader.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 7.5)
$LblLogHeader.ForeColor = $TEAL
$LblLogHeader.BackColor = [System.Drawing.Color]::FromArgb(0, 30, 28)
$LblLogHeader.Location  = New-Object System.Drawing.Point(685, 86)
$LblLogHeader.Size      = New-Object System.Drawing.Size(431, 20)
$LblLogHeader.TextAlign = "MiddleLeft"
$Form.Controls.Add($LblLogHeader)

$TxtLog = New-Object System.Windows.Forms.RichTextBox
$TxtLog.Location    = New-Object System.Drawing.Point(685, 110)
$TxtLog.Size        = New-Object System.Drawing.Size(431, 788)
$TxtLog.BackColor   = $PANEL
$TxtLog.ForeColor   = $FG_DIM
$TxtLog.Font        = $FONT_LOG
$TxtLog.ReadOnly    = $true
$TxtLog.BorderStyle = "None"
$TxtLog.ScrollBars  = "Vertical"
$Form.Controls.Add($TxtLog)

# ============================================================
#  RUNTIME HELPERS
# ============================================================
function AppendLog($msg, $color) {
    $TxtLog.SelectionStart  = $TxtLog.TextLength
    $TxtLog.SelectionLength = 0
    $TxtLog.SelectionColor  = $color
    $TxtLog.AppendText("$msg`n")
    $TxtLog.ScrollToCaret()
    [System.Windows.Forms.Application]::DoEvents()
}

function ResetUI {
    $BtnDryRun.Enabled     = $true
    SetLiveIngestEnabled $script:DryRunCompleted
    SetOpenFolderEnabled $script:VaultFolderReady
    SetVerifyEnabled $true
    $BtnStop.Enabled       = $false
    $BtnDryRun.Text        = "START DRY RUN"
    $BtnLiveIngest.Text    = "START LIVE INGEST"
    $BtnDryRun.BackColor   = $PANEL
    $LblSpeed.Text       = ""
    $LblETA.Text         = ""
    $LblCurrentFile.Text = ""
}

# -- THUMBNAIL PREVIEW -----------------------------------------
# Pulls each file's embedded preview/thumbnail tag straight out of its EXIF
# data via ExifTool (fast - no full-resolution decode needed) and renders a
# small filmstrip of what's about to be copied.
$THUMB_SIZE   = 76
$script:ThumbBoxesByName = @{}

function ExtractThumbnailBytes($exiftoolPath, $filePath, $tagName) {
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName               = $exiftoolPath
        $psi.Arguments              = "-b -$tagName -q -q `"$filePath`""
        $psi.RedirectStandardOutput = $true
        $psi.UseShellExecute        = $false
        $psi.CreateNoWindow         = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        $ms = New-Object System.IO.MemoryStream
        $proc.StandardOutput.BaseStream.CopyTo($ms)
        $proc.WaitForExit()
        if ($ms.Length -eq 0) { return $null }
        return $ms.ToArray()
    } catch { return $null }
}

function MakeThumbBitmap($size, $bytes) {
    if (-not $bytes -or $bytes.Length -eq 0) { return $null }
    try {
        $ms   = New-Object System.IO.MemoryStream(,$bytes)
        $orig = [System.Drawing.Image]::FromStream($ms)
        $ratio = [Math]::Min($size / $orig.Width, $size / $orig.Height)
        $w = [Math]::Max(1, [int]($orig.Width * $ratio))
        $h = [Math]::Max(1, [int]($orig.Height * $ratio))
        $bmp = New-Object System.Drawing.Bitmap($size, $size)
        $g   = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = "HighQualityBicubic"
        $g.Clear($PANEL)
        $g.DrawImage($orig, [int](($size - $w) / 2), [int](($size - $h) / 2), $w, $h)
        $g.Dispose()
        $orig.Dispose()
        $ms.Dispose()
        return $bmp
    } catch { return $null }
}

function MakePlaceholderBitmap($size, $ext) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size)
    $g   = [System.Drawing.Graphics]::FromImage($bmp)
    $g.Clear($PANEL)
    $g.DrawRectangle((New-Object System.Drawing.Pen($FG_DIM)), 1, 1, $size - 2, $size - 2)
    $fmt = New-Object System.Drawing.StringFormat
    $fmt.Alignment     = "Center"
    $fmt.LineAlignment = "Center"
    $rect  = New-Object System.Drawing.RectangleF(0, 0, $size, $size)
    $brush = New-Object System.Drawing.SolidBrush($FG_DIM)
    $g.DrawString($ext.ToUpper(), $FONT_SUB, $brush, $rect, $fmt)
    $g.Dispose()
    $brush.Dispose()
    return $bmp
}

# Highlights the thumbnail of the file currently being copied/simulated.
function HighlightThumbnail($fileName) {
    if ($script:ThumbLastHighlighted -and $script:ThumbBoxesByName.ContainsKey($script:ThumbLastHighlighted)) {
        $script:ThumbBoxesByName[$script:ThumbLastHighlighted].BackColor = $BG
    }
    if ($script:ThumbBoxesByName.ContainsKey($fileName)) {
        $script:ThumbBoxesByName[$fileName].BackColor = $TEAL_DIM
        $script:ThumbLastHighlighted = $fileName
    }
}

# Builds the filmstrip for a bounded, chronologically-sorted sample of the
# files queued for copy. Capped so a huge card doesn't stall the UI pulling
# thumbnails one exiftool call at a time.
function GenerateThumbnailStrip($allFiles, $audioFiles, $exiftoolPath) {
    $ThumbnailStrip.SuspendLayout()
    $ThumbnailStrip.Controls.Clear()
    $script:ThumbBoxesByName    = @{}
    $script:ThumbLastHighlighted = $null

    $cap    = 50
    $sample = @($allFiles | Sort-Object LastWriteTime | Select-Object -First $cap)
    $rest   = [Math]::Max(0, $allFiles.Count - $sample.Count) + $audioFiles.Count
    $exiftoolOk = $exiftoolPath -and (Test-Path $exiftoolPath)

    $LblStatus.Text = "Generating previews..."
    foreach ($f in $sample) {
        [System.Windows.Forms.Application]::DoEvents()
        $ext = $f.Extension.TrimStart(".").ToLower()

        $bmp = $null
        if ($exiftoolOk) {
            $bytes = ExtractThumbnailBytes $exiftoolPath $f.FullName "ThumbnailImage"
            if (-not $bytes) { $bytes = ExtractThumbnailBytes $exiftoolPath $f.FullName "PreviewImage" }
            if ($bytes) { $bmp = MakeThumbBitmap $THUMB_SIZE $bytes }
        }
        if (-not $bmp) { $bmp = MakePlaceholderBitmap $THUMB_SIZE $ext }

        $cell = New-Object System.Windows.Forms.Panel
        $cell.Size      = New-Object System.Drawing.Size(($THUMB_SIZE + 8), 108)
        $cell.BackColor = $BG
        $cell.Margin    = New-Object System.Windows.Forms.Padding(2)

        $pic = New-Object System.Windows.Forms.PictureBox
        $pic.Image     = $bmp
        $pic.Size      = New-Object System.Drawing.Size($THUMB_SIZE, $THUMB_SIZE)
        $pic.Location  = New-Object System.Drawing.Point(4, 4)
        $pic.SizeMode  = "Zoom"
        $pic.BackColor = $PANEL
        $cell.Controls.Add($pic)

        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text      = $f.Name
        $lbl.Font      = $FONT_SUB
        $lbl.ForeColor = $FG_DIM
        $lbl.Location  = New-Object System.Drawing.Point(2, ($THUMB_SIZE + 6))
        $lbl.Size      = New-Object System.Drawing.Size(($THUMB_SIZE + 4), 28)
        $lbl.TextAlign = "TopCenter"
        $cell.Controls.Add($lbl)

        $ThumbnailStrip.Controls.Add($cell)
        $script:ThumbBoxesByName[$f.Name] = $cell
    }

    if ($rest -gt 0) {
        $more = New-Object System.Windows.Forms.Label
        $more.Text      = "+$rest more"
        $more.Font      = $FONT_SUB
        $more.ForeColor = $FG_DIM
        $more.Size      = New-Object System.Drawing.Size(80, $THUMB_SIZE)
        $more.TextAlign = "MiddleCenter"
        $more.Margin    = New-Object System.Windows.Forms.Padding(6, 2, 2, 2)
        $ThumbnailStrip.Controls.Add($more)
    }
    $ThumbnailStrip.ResumeLayout()
}

# -- SD AUTO-DETECT ------------------------------------------
# Drive queries (IsReady / Test-Path on a card reader or sleeping disk) can block for
# seconds, which froze the window at startup ("Not Responding"). The scan therefore
# runs in a background runspace and a timer applies the result on the UI thread.
$DetectSDCardScript = {
    foreach ($drive in [System.IO.DriveInfo]::GetDrives()) {
        if ($drive.DriveType -eq "Removable" -and $drive.IsReady) {
            if (Test-Path (Join-Path $drive.RootDirectory.FullName "DCIM")) {
                $label = $drive.VolumeLabel
                if (-not $label) { $label = "(no label)" }
                return @{ Letter = $drive.Name.TrimEnd("\"); Label = $label }
            }
        }
    }
    return $null
}

function StartSDDetect {
    if ($script:DetectTimer -and $script:DetectTimer.Enabled) { return }   # scan already running
    $LblAutoDetect.Text      = "Detecting SD card..."
    $LblAutoDetect.ForeColor = $FG_DIM
    $script:DetectPS = [powershell]::Create()
    [void]$script:DetectPS.AddScript($DetectSDCardScript)
    $script:DetectHandle = $script:DetectPS.BeginInvoke()
    $script:DetectTimer = New-Object System.Windows.Forms.Timer
    $script:DetectTimer.Interval = 250
    $script:DetectTimer.Add_Tick({
        if (-not $script:DetectHandle.IsCompleted) { return }
        $script:DetectTimer.Stop()
        $found = $null
        try { $found = @($script:DetectPS.EndInvoke($script:DetectHandle))[0] } catch {}
        $script:DetectPS.Dispose()
        if ($found) {
            $item = $found.Letter + "\"
            if ($CmbDrive.Items.Contains($item)) { $CmbDrive.SelectedItem = $item }
            $LblAutoDetect.Text      = "Auto-detected: $($found.Letter) `"$($found.Label)`" (DCIM found)"
            $LblAutoDetect.ForeColor = $GREEN
        } else {
            $LblAutoDetect.Text      = "No SD card detected - select manually"
            $LblAutoDetect.ForeColor = $YELLOW
        }
    })
    $script:DetectTimer.Start()
}

$Form.Add_Shown({ StartSDDetect })
$CmbDrive.Add_DropDown({ StartSDDetect })   # re-scan when the list is opened (card inserted after launch)

# -- STOP & MULTI-SHOOT STATE --------------------------------
$script:StopRequested      = $false
$script:CurrentProcess     = $null
$script:MultipleShoots     = @()
$script:FileToShootMapping = @{}

# Live Ingest is locked until a dry run completes successfully in this session,
# so a card never gets ingested for real without a preview pass first.
$script:DryRunCompleted = $false
SetLiveIngestEnabled $false
$LiveIngestToolTip = New-Object System.Windows.Forms.ToolTip
$LiveIngestToolTip.SetToolTip($BtnLiveIngest, "Run a Dry Run first to preview what will be copied")

# Open Vault Folder unlocks once a live ingest has copied files somewhere.
$script:VaultFolderReady = $false
$script:LastIngestFolder = $null
SetOpenFolderEnabled $false

$BtnStop.Add_Click({
    $script:StopRequested = $true
    if ($script:CurrentProcess -and -not $script:CurrentProcess.HasExited) {
        try { $script:CurrentProcess.Kill() } catch {}
    }
    AppendLog "[WARN]  Stopped by user." $RED
    $LblStatus.Text      = "Stopped."
    $LblStatus.ForeColor = $RED
    ResetUI
})

# -- DUPLICATE INDEX -----------------------------------------
function BuildDestIndex($destPath) {
    $index = @{}
    if (Test-Path $destPath) {
        Get-ChildItem -Path $destPath -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
            $key = $_.Name.ToLower()
            if (-not $index.ContainsKey($key)) { $index[$key] = @{ Length = $_.Length; Path = $_.FullName } }
        }
    }
    return $index
}

# A file only counts as a duplicate when the recycled filename AND size match -
# Nikon bodies wrap the DSC_/file counter back to 0001 after ~9999 shots, so
# unrelated files can share a name across shoots. Name+size is just a cheap
# pre-filter though, so once both match we confirm with an MD5 checksum
# against the actual vault file before calling it a true duplicate.
function IsDuplicateInVault($destIndex, $key, $sourceLength, $sourcePath) {
    if (-not $destIndex.ContainsKey($key)) { return $false }
    $entry = $destIndex[$key]
    if ($entry.Length -ne $sourceLength) { return $false }
    if (-not $sourcePath -or -not (Test-Path $sourcePath) -or -not (Test-Path $entry.Path)) { return $false }
    $srcHash  = (Get-FileHash -Path $sourcePath -Algorithm MD5).Hash
    $destHash = (Get-FileHash -Path $entry.Path -Algorithm MD5).Hash
    return $srcHash -eq $destHash
}

# -- EXIFTOOL RUNNER -----------------------------------------
# Helper to get the right suffix for a file (shoot-specific or global)
function GetSuffixForFile($fileFullName) {
    if ($script:FileToShootMapping -and $script:FileToShootMapping.ContainsKey($fileFullName)) {
        $shootIndex = $script:FileToShootMapping[$fileFullName]
        if ($script:ShootDescriptions -and $script:ShootDescriptions.ContainsKey($shootIndex)) {
            return " " + $script:ShootDescriptions[$shootIndex]
        }
    }
    return $script:Suffix
}

# $subFolder: media-type subfolder under RAW (e.g. "CAM_RAW", "VIDEO", "AUDIO", "MISC")
# $sourceFiles: pre-gathered FileInfo list for this step (e.g. $RawFiles)
function RunExifStep($sourceFiles, $destIndex, $isDryRun, $dest, $subFolder) {
    $destPattern = "$dest\%Y_%m\%Y_%m_%d`$($script:Suffix)\RAW\$subFolder"

    if ($isDryRun) {
        foreach ($f in $sourceFiles) {
            if ($script:StopRequested) { return $false }
            $key = $f.Name.ToLower()
            if (IsDuplicateInVault $destIndex $key $f.Length $f.FullName) {
                AppendLog ("  [SKIP - duplicate]  " + $f.Name) $ORANGE
            } else {
                $subLabel = if ($subFolder -ne "") { " >> $subFolder" } else { "" }
                AppendLog ("  [WOULD COPY$subLabel]  " + $f.Name + "  (" + (FormatBytes $f.Length) + ")") $TEAL_DIM
            }
            $script:BytesDone += $f.Length
            $script:FilesDone++
            $pct = if ($script:TotalBytes -gt 0) { [int](($script:BytesDone / $script:TotalBytes) * 100) } else { 0 }
            $ProgressBar.Value   = [Math]::Min($pct, 100)
            $LblPct.Text         = "$([Math]::Min($pct,100))%"
            $LblFiles.Text       = "$($script:FilesDone) / $($script:TotalFiles) files"
            $LblCurrentFile.Text = "Simulating: $($f.Name)"
            HighlightThumbnail $f.Name
            [System.Windows.Forms.Application]::DoEvents()
        }
        return $true
    }

    # Filter out true duplicates (name+size+checksum) BEFORE invoking ExifTool,
    # so already-vaulted files are neither re-copied nor overwritten.
    $toCopy = @()
    foreach ($f in $sourceFiles) {
        $key = $f.Name.ToLower()
        if (IsDuplicateInVault $destIndex $key $f.Length $f.FullName) {
            AppendLog ("  [DUPLICATE - already in vault]  " + $f.Name) $ORANGE
            $script:BytesDone += $f.Length
            $script:FilesDone++
        } else {
            $toCopy += $f
        }
    }
    if ($toCopy.Count -eq 0) { return $true }

    # One exiftool invocation per file (rather than one batch call for the whole
    # step) so the progress bar/labels advance after every file instead of
    # sitting still until the entire step's output finally flushes.
    foreach ($f in $toCopy) {
        if ($script:StopRequested) { return $false }

        HighlightThumbnail $f.Name
        $LblCurrentFile.Text = "Copying: $($f.Name)  (" + (FormatBytes $f.Length) + ")"
        [System.Windows.Forms.Application]::DoEvents()

        $fileSuffix = GetSuffixForFile $f.FullName
        $fileDestPattern = "$dest\%Y_%m\%Y_%m_%d$($fileSuffix)\RAW\$subFolder"
        # Try with metadata tags first. Some files (e.g. Insta360 MP4/LRV with a proprietary
        # trailer) can't be tagged - ExifTool then skips the copy entirely - so retry once
        # as a plain date-organised copy without tags rather than losing the file.
        foreach ($withMeta in @($true, $false)) {
            $metaArgs = if ($withMeta) { $script:MetadataArgs } else { "" }
            $argStr = "-d `"$fileDestPattern`" `"-Directory<DateTimeOriginal`" `"-Directory<CreateDate`" `"-Directory<FileModifyDate`" $metaArgs -o `".`" --overwrite_original `"$($f.FullName)`""

            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName               = $ExifBox.TextBox.Text.Trim()
            $psi.Arguments              = $argStr
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError  = $true
            $psi.UseShellExecute        = $false
            $psi.CreateNoWindow         = $true

            $script:CurrentProcess = New-Object System.Diagnostics.Process
            $script:CurrentProcess.StartInfo = $psi
            $script:CurrentProcess.Start() | Out-Null
            $errText = $script:CurrentProcess.StandardError.ReadToEnd()
            $outText = $script:CurrentProcess.StandardOutput.ReadToEnd()
            $script:CurrentProcess.WaitForExit()

            if ($script:StopRequested) { return $false }

            $failed = ($outText -match "weren't updated due to errors")
            if (-not $failed -or -not $withMeta -or $script:MetadataArgs -eq "") { break }
            AppendLog "  [WARN]  Could not write metadata to $($f.Name) - copying without tags." $YELLOW
        }

        foreach ($line in ($outText -split "`r?`n")) {
            $trimmed = $line.Trim()
            if ($trimmed -match "^(Error|Warning)") { AppendLog "  $trimmed" $YELLOW }
        }
        if ($errText.Trim() -ne "") { AppendLog "  $($errText.Trim())" $YELLOW }

        $script:BytesDone += $f.Length
        $script:FilesDone++
        $pct = if ($script:TotalBytes -gt 0) { [int](($script:BytesDone / $script:TotalBytes) * 100) } else { 0 }
        $ProgressBar.Value   = [Math]::Min($pct, 100)
        $LblPct.Text         = "$([Math]::Min($pct,100))%"
        $LblFiles.Text       = "$($script:FilesDone) / $($script:TotalFiles) files"
        $elapsed = ([DateTime]::Now - $script:IngestStart).TotalSeconds
        if ($elapsed -gt 1 -and $script:BytesDone -gt 0) {
            $speed     = $script:BytesDone / $elapsed
            $remaining = $script:TotalBytes - $script:BytesDone
            $etaSecs   = if ($speed -gt 0) { $remaining / $speed } else { 0 }
            $LblSpeed.Text = FormatSpeed $speed
            $LblETA.Text   = "ETA  " + (FormatETA $etaSecs)
        }
        AppendLog ("  [" + (FormatBytes $script:BytesDone) + " / " + (FormatBytes $script:TotalBytes) + "]  $($f.Name)") $FG_DIM
        [System.Windows.Forms.Application]::DoEvents()
    }
    return (-not $script:StopRequested)
}

# -- VERIFY --------------------------------------------------
# vault file name (lowercase) -> list of FileInfo, so each source file is a hash lookup
function BuildVaultNameIndex($destPath) {
    $idx = @{}
    Get-ChildItem -Path $destPath -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        $k = $_.Name.ToLower()
        if (-not $idx.ContainsKey($k)) { $idx[$k] = New-Object System.Collections.ArrayList }
        [void]$idx[$k].Add($_)
    }
    return $idx
}

# Streaming SHA-256 that keeps the UI alive, advances the progress bar and honours STOP.
# Returns $null if stopped.
$script:VerifyBytesDone  = 0
$script:VerifyBytesTotal = 0
function HashFileResponsive($path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $fs  = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $buf = New-Object byte[] 4194304
        while (($n = $fs.Read($buf, 0, $buf.Length)) -gt 0) {
            if ($script:StopRequested) { return $null }
            [void]$sha.TransformBlock($buf, 0, $n, $null, 0)
            $script:VerifyBytesDone += $n
            if ($clock.ElapsedMilliseconds -gt 200) {
                $clock.Restart()
                if ($script:VerifyBytesTotal -gt 0) {
                    $pct = [Math]::Min([int](($script:VerifyBytesDone / $script:VerifyBytesTotal) * 100), 100)
                    $ProgressBar.Value = $pct
                    $LblPct.Text       = "$pct%"
                }
                [System.Windows.Forms.Application]::DoEvents()
            }
        }
        [void]$sha.TransformFinalBlock($buf, 0, 0)
        return [BitConverter]::ToString($sha.Hash)
    } finally {
        $fs.Dispose(); $sha.Dispose()
    }
}

# ExifTool's hash of just the image data - identical before/after a metadata-only
# rewrite, which is how a Copyright-tagged photo can be proven intact even though
# its bytes (size/hash) legitimately differ from the card. Empty if unsupported.
function GetImageDataMD5($exiftoolPath, $path) {
    if (-not $exiftoolPath -or -not (Test-Path $exiftoolPath)) { return "" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $exiftoolPath
    $psi.Arguments              = "-m -s3 -ImageDataMD5 `"$path`""
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $script:CurrentProcess = New-Object System.Diagnostics.Process
    $script:CurrentProcess.StartInfo = $psi
    $script:CurrentProcess.Start() | Out-Null
    $out = $script:CurrentProcess.StandardOutput.ReadToEnd().Trim()
    $script:CurrentProcess.WaitForExit()
    if ($out -match '^[0-9a-fA-F]{32}$') { return $out.ToLower() }
    return ""
}

# Compares one card file against its vault candidates. Returns one of:
#   OK        same size (and, if $doHash, same SHA-256)
#   OK-TAGGED bytes differ but image data is identical (metadata-only difference)
#   MISSING / SIZE / HASH / STOPPED
function VerifyFilePair($srcFile, $candidates, $exiftoolPath, $doHash) {
    if (-not $candidates -or $candidates.Count -eq 0) { return "MISSING" }
    $same = @($candidates | Where-Object { $_.Length -eq $srcFile.Length })
    if ($same.Count -gt 0) {
        if (-not $doHash) { return "OK" }
        $srcHash = HashFileResponsive $srcFile.FullName
        if ($null -eq $srcHash) { return "STOPPED" }
        foreach ($c in $same) {
            $h = HashFileResponsive $c.FullName
            if ($null -eq $h) { return "STOPPED" }
            if ($h -eq $srcHash) { return "OK" }
        }
        # Same size but different bytes: could still be a same-length metadata rewrite.
        $srcMd5 = GetImageDataMD5 $exiftoolPath $srcFile.FullName
        if ($srcMd5 -ne "") {
            foreach ($c in $same) {
                if ((GetImageDataMD5 $exiftoolPath $c.FullName) -eq $srcMd5) { return "OK-TAGGED" }
            }
        }
        return "HASH"
    }
    $srcMd5 = GetImageDataMD5 $exiftoolPath $srcFile.FullName
    if ($script:StopRequested) { return "STOPPED" }
    if ($srcMd5 -ne "") {
        foreach ($c in $candidates) {
            if ((GetImageDataMD5 $exiftoolPath $c.FullName) -eq $srcMd5) { return "OK-TAGGED" }
        }
    }
    return "SIZE"
}

# Most common non-empty Copyright tag among the first few card files ("" if none).
function DetectCopyright($files, $exiftoolPath) {
    if (-not $exiftoolPath -or -not (Test-Path $exiftoolPath)) { return "" }
    $sample = @($files | Select-Object -First 20)
    if ($sample.Count -eq 0) { return "" }
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $exiftoolPath
    # -p prints values only; -s3 with several files also emits "======== path" headers,
    # which were being picked up as the "most common copyright".
    $psi.Arguments              = "-m -p `"`$Copyright`" " +(($sample | ForEach-Object { "`"$($_.FullName)`"" }) -join " ")
    $psi.RedirectStandardOutput = $true
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $script:CurrentProcess = New-Object System.Diagnostics.Process
    $script:CurrentProcess.StartInfo = $psi
    $script:CurrentProcess.Start() | Out-Null
    $out = $script:CurrentProcess.StandardOutput.ReadToEnd()
    $script:CurrentProcess.WaitForExit()
    $vals = @($out -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
    if ($vals.Count -eq 0) { return "" }
    return ($vals | Group-Object | Sort-Object Count -Descending | Select-Object -First 1).Name
}

function VerifyIngest($sourceFiles, $destPath, $isDryRun, $exiftoolPath) {
    if ($isDryRun) { AppendLog "[INFO]  Skipping verification (dry run)." $FG_DIM; return }
    AppendLog "`n[INFO]  Verifying ingest..." $YELLOW
    $srcCount = $sourceFiles.Count
    $srcBytes = ($sourceFiles | Measure-Object -Property Length -Sum).Sum
    if (-not $srcBytes) { $srcBytes = 0 }
    $matchedCount = 0; $matchedBytes = 0; $missing = @()
    $vaultIndex = BuildVaultNameIndex $destPath
    foreach ($f in $sourceFiles) {
        # Match on name AND size (or identical image data for tagged files), so a
        # truncated/partial copy is reported rather than passed.
        $status = VerifyFilePair $f $vaultIndex[$f.Name.ToLower()] $exiftoolPath $false
        if ($status -like "OK*") { $matchedCount++; $matchedBytes += $f.Length }
        else { $missing += $f.Name }
    }
    AppendLog ("  Source : $srcCount files  (" + (FormatBytes $srcBytes) + ")") $FG
    AppendLog ("  Dest   : $matchedCount files matched  (" + (FormatBytes $matchedBytes) + ")") $FG
    if ($missing.Count -gt 0) {
        AppendLog "[WARN]  $($missing.Count) file(s) missing or incomplete in destination:" $RED
        foreach ($m in $missing) { AppendLog "    MISSING: $m" $RED }
        AppendLog "[WARN]  Do NOT format SD card until resolved." $RED
    } elseif ($matchedCount -eq $srcCount) {
        AppendLog "[OK]    All $srcCount files verified." $GREEN
        AppendLog "[OK]    Safe to format SD card." $GREEN
    }
}

# -- MULTIPLE SHOOTS DETECTION & CONTACT SHEET ---------------
# Groups files by EXIF date, then detects multiple shoots (30+ min gaps)
# Returns an array of shoot groups: @{ Date, ShootIndex, Files }
function DetectShoots($allPhotoFiles, $exiftoolPath) {
    if ($allPhotoFiles.Count -eq 0) { return @() }

    $exiftoolOk = $exiftoolPath -and (Test-Path $exiftoolPath)
    if (-not $exiftoolOk) { return @() }

    $shoots = @()
    $filesByDateTime = @()

    foreach ($f in $allPhotoFiles) {
        [System.Windows.Forms.Application]::DoEvents()
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName               = $exiftoolPath
            $psi.Arguments              = "-DateTimeOriginal -s -s -q `"$($f.FullName)`""
            $psi.RedirectStandardOutput = $true
            $psi.UseShellExecute        = $false
            $psi.CreateNoWindow         = $true
            $proc = [System.Diagnostics.Process]::Start($psi)
            $outText = $proc.StandardOutput.ReadToEnd()
            $proc.WaitForExit()

            $dtStr = $outText.Trim()
            if ($dtStr -match '(\d{4}):(\d{2}):(\d{2})\s+(\d{2}):(\d{2}):(\d{2})') {
                $dt = [DateTime]::Parse("$($Matches[1])-$($Matches[2])-$($Matches[3]) $($Matches[4]):$($Matches[5]):$($Matches[6])")
                $filesByDateTime += @{ File = $f; DateTime = $dt }
            }
        } catch {}
    }

    if ($filesByDateTime.Count -eq 0) { return @() }

    $filesByDateTime = $filesByDateTime | Sort-Object { $_.DateTime }
    $currentShoot = @()
    $lastDateTime = $null
    $shootIndex = 0

    foreach ($item in $filesByDateTime) {
        if ($null -eq $lastDateTime) {
            $currentShoot += $item.File
            $lastDateTime = $item.DateTime
        } else {
            $timeDiff = ($item.DateTime - $lastDateTime).TotalSeconds
            if ($timeDiff -gt 1800) {  # 30 minutes = 1800 seconds
                if ($currentShoot.Count -gt 0) {
                    $shoots += @{ Files = $currentShoot; ShootIndex = $shootIndex; Date = $lastDateTime }
                    $shootIndex++
                }
                $currentShoot = @($item.File)
                $lastDateTime = $item.DateTime
            } else {
                $currentShoot += $item.File
                $lastDateTime = $item.DateTime
            }
        }
    }
    if ($currentShoot.Count -gt 0) {
        $shoots += @{ Files = $currentShoot; ShootIndex = $shootIndex; Date = $lastDateTime }
    }

    # Force array output: a single-hashtable array would otherwise unwrap on
    # the pipeline, making callers see a hashtable (Count = key count) instead
    # of a one-element shoot array (Count = 1).
    return ,$shoots
}

# Modal dialog: show contact sheet for multiple shoots, let user assign each to a folder
function ShowShootSelectionDialog($shoots, $exiftoolPath) {
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text            = "Multiple Shoots Detected"
    $dlg.Size            = New-Object System.Drawing.Size(900, 600)
    $dlg.StartPosition   = "CenterParent"
    $dlg.BackColor       = $BG
    $dlg.ForeColor       = $FG
    $dlg.Font            = $FONT_UI
    $dlg.ShowInTaskbar   = $false
    $dlg.TopMost         = $true

    $lbl = New-Object System.Windows.Forms.Label
    $lbl.Text      = "Multiple shoots detected. Select which photos belong to each shoot folder."
    $lbl.Font      = New-Object System.Drawing.Font("Segoe UI", 9)
    $lbl.ForeColor = $FG_DIM
    $lbl.Location  = New-Object System.Drawing.Point(12, 12)
    $lbl.Size      = New-Object System.Drawing.Size(876, 20)
    $dlg.Controls.Add($lbl)

    $pnlShoot = New-Object System.Windows.Forms.Panel
    $pnlShoot.Location    = New-Object System.Drawing.Point(12, 38)
    $pnlShoot.Size        = New-Object System.Drawing.Size(876, 450)
    $pnlShoot.BackColor   = $PANEL
    $pnlShoot.AutoScroll  = $true
    $dlg.Controls.Add($pnlShoot)

    $yPos = 6
    foreach ($shoot in $shoots) {
        $shootGroup = New-Object System.Windows.Forms.GroupBox
        $shootGroup.Text      = "Shoot $($shoot.ShootIndex + 1) - $($shoot.Date.ToString('HH:mm:ss')) ($($shoot.Files.Count) files)"
        $shootGroup.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 8.5)
        $shootGroup.ForeColor = $TEAL
        $shootGroup.BackColor = [System.Drawing.Color]::FromArgb(22, 30, 40)
        $shootGroup.Location  = New-Object System.Drawing.Point(6, $yPos)
        $shootGroup.Size      = New-Object System.Drawing.Size(864, 130)

        $thumbnails = New-Object System.Windows.Forms.FlowLayoutPanel
        $thumbnails.Location      = New-Object System.Drawing.Point(6, 18)
        $thumbnails.Size          = New-Object System.Drawing.Size(852, 100)
        $thumbnails.FlowDirection = "LeftToRight"
        $thumbnails.AutoScroll    = $true
        $thumbnails.WrapContents  = $false
        $thumbnails.BackColor     = [System.Drawing.Color]::FromArgb(13, 17, 23)

        foreach ($f in $shoot.Files | Select-Object -First 12) {
            [System.Windows.Forms.Application]::DoEvents()
            $thumb = New-Object System.Windows.Forms.Panel
            $thumb.Size      = New-Object System.Drawing.Size(84, 84)
            $thumb.BackColor = [System.Drawing.Color]::FromArgb(40, 50, 60)
            $thumb.Margin    = New-Object System.Windows.Forms.Padding(2)

            $pic = New-Object System.Windows.Forms.PictureBox
            $pic.Size      = New-Object System.Drawing.Size(80, 80)
            $pic.Location  = New-Object System.Drawing.Point(2, 2)
            $pic.BackColor = $PANEL

            $ext = $f.Extension.TrimStart(".").ToLower()
            $picBmp = $null

            if ($exiftoolPath -and (Test-Path $exiftoolPath)) {
                $bytes = ExtractThumbnailBytes $exiftoolPath $f.FullName "ThumbnailImage"
                if (-not $bytes) { $bytes = ExtractThumbnailBytes $exiftoolPath $f.FullName "PreviewImage" }
                if ($bytes) { $picBmp = MakeThumbBitmap 80 $bytes }
            }
            if (-not $picBmp) { $picBmp = MakePlaceholderBitmap 80 $ext }

            $pic.Image = $picBmp
            $pic.SizeMode = "Zoom"
            $thumb.Controls.Add($pic)
            $thumbnails.Controls.Add($thumb)
        }
        $shootGroup.Controls.Add($thumbnails)
        $pnlShoot.Controls.Add($shootGroup)
        $yPos += 140
    }

    $btnOK = New-Object System.Windows.Forms.Button
    $btnOK.Text      = "Create Separate Folders"
    $btnOK.Location  = New-Object System.Drawing.Point(12, 500)
    $btnOK.Size      = New-Object System.Drawing.Size(180, 32)
    $btnOK.BackColor = $TEAL
    $btnOK.ForeColor = $BG
    $btnOK.FlatStyle = "Flat"
    $btnOK.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
    $btnOK.Cursor    = "Hand"
    $btnOK.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $dlg.Controls.Add($btnOK)

    $btnCancel = New-Object System.Windows.Forms.Button
    $btnCancel.Text      = "Merge to Single Folder"
    $btnCancel.Location  = New-Object System.Drawing.Point(202, 500)
    $btnCancel.Size      = New-Object System.Drawing.Size(180, 32)
    $btnCancel.BackColor = $PANEL
    $btnCancel.ForeColor = $FG_DIM
    $btnCancel.FlatStyle = "Flat"
    $btnCancel.Font      = New-Object System.Drawing.Font("Segoe UI Semibold", 9)
    $btnCancel.FlatAppearance.BorderColor = $FG_DIM
    $btnCancel.FlatAppearance.BorderSize  = 1
    $btnCancel.Cursor    = "Hand"
    $btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $dlg.Controls.Add($btnCancel)

    $result = $dlg.ShowDialog($Form)
    $dlg.Dispose()

    return $result -eq [System.Windows.Forms.DialogResult]::OK
}

# Dialog to get job description(s) after multi-shoot selection
function ShowJobDescriptionDialog($shootCount, $useMultipleFolders) {
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text            = "Job Description"
    $formHeight = if ($useMultipleFolders -and $shootCount -gt 1) { 150 + ($shootCount * 50) } else { 180 }
    $dlg.Size            = New-Object System.Drawing.Size(500, $formHeight)
    $dlg.StartPosition   = "CenterParent"
    $dlg.BackColor       = $BG
    $dlg.ForeColor       = $FG
    $dlg.Font            = $FONT_UI
    $dlg.FormBorderStyle = "FixedDialog"
    $dlg.MaximizeBox     = $false
    $dlg.MinimizeBox     = $false

    $y = 20
    $shootDescriptions = @()

    if ($useMultipleFolders -and $shootCount -gt 1) {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text      = "Name each shoot:"
        $lbl.Font      = $FONT_LBL
        $lbl.ForeColor = $FG_DIM
        $lbl.Location  = New-Object System.Drawing.Point(20, $y)
        $lbl.Size      = New-Object System.Drawing.Size(460, 18)
        $dlg.Controls.Add($lbl)
        $y += 30

        for ($i = 0; $i -lt $shootCount; $i++) {
            $lbl = New-Object System.Windows.Forms.Label
            $lbl.Text      = "Shoot $($i + 1):"
            $lbl.Font      = $FONT_LBL
            $lbl.ForeColor = $FG_DIM
            $lbl.Location  = New-Object System.Drawing.Point(20, $y)
            $lbl.Size      = New-Object System.Drawing.Size(80, 18)
            $dlg.Controls.Add($lbl)

            $txt = New-Object System.Windows.Forms.TextBox
            $txt.Location    = New-Object System.Drawing.Point(120, ($y - 2))
            $txt.Size        = New-Object System.Drawing.Size(360, 26)
            $txt.BackColor   = $PANEL
            $txt.ForeColor   = $FG
            $txt.BorderStyle = "FixedSingle"
            $txt.Font        = $FONT_UI
            $txt.Tag         = $i
            $dlg.Controls.Add($txt)
            $shootDescriptions += $txt
            $y += 35
        }
    } else {
        $lbl = New-Object System.Windows.Forms.Label
        $lbl.Text      = "Job Description:"
        $lbl.Font      = $FONT_LBL
        $lbl.ForeColor = $FG_DIM
        $lbl.Location  = New-Object System.Drawing.Point(20, 20)
        $lbl.Size      = New-Object System.Drawing.Size(460, 18)
        $dlg.Controls.Add($lbl)

        $txt = New-Object System.Windows.Forms.TextBox
        $txt.Location    = New-Object System.Drawing.Point(20, 50)
        $txt.Size        = New-Object System.Drawing.Size(460, 26)
        $txt.BackColor   = $PANEL
        $txt.ForeColor   = $FG
        $txt.BorderStyle = "FixedSingle"
        $txt.Font        = $FONT_UI
        $dlg.Controls.Add($txt)
        $shootDescriptions += $txt
        $y = 90
    }

    $okBtn = New-Object System.Windows.Forms.Button
    $okBtn.Text      = "OK"
    $okBtn.Location  = New-Object System.Drawing.Point(300, $y)
    $okBtn.Size      = New-Object System.Drawing.Size(90, 32)
    $okBtn.BackColor = $PANEL
    $okBtn.ForeColor = $TEAL
    $okBtn.FlatStyle = "Flat"
    $okBtn.FlatAppearance.BorderColor = $TEAL
    $okBtn.FlatAppearance.BorderSize  = 1
    $okBtn.Cursor    = "Hand"
    $okBtn.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $dlg.Controls.Add($okBtn)

    $cancelBtn = New-Object System.Windows.Forms.Button
    $cancelBtn.Text      = "Cancel"
    $cancelBtn.Location  = New-Object System.Drawing.Point(400, $y)
    $cancelBtn.Size      = New-Object System.Drawing.Size(90, 32)
    $cancelBtn.BackColor = $PANEL
    $cancelBtn.ForeColor = $FG_DIM
    $cancelBtn.FlatStyle = "Flat"
    $cancelBtn.FlatAppearance.BorderColor = $FG_DIM
    $cancelBtn.FlatAppearance.BorderSize  = 1
    $cancelBtn.Cursor    = "Hand"
    $cancelBtn.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $dlg.Controls.Add($cancelBtn)

    $dlg.AcceptButton = $okBtn
    $dlg.CancelButton = $cancelBtn

    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
        # Comma operator forces array-in-pipeline: with a single textbox, a
        # bare @(...) still unwraps to a scalar string on return, and
        # $result[0] would then index the string's first CHAR, not the
        # array's first element.
        return ,@($shootDescriptions | ForEach-Object { $_.Text.Trim() })
    }
    return ,@()
}

# ============================================================
#  RUN BUTTONS
# ============================================================
function StartIngest($IsDryRun) {
    if (-not $IsDryRun -and -not $script:DryRunCompleted) {
        AppendLog "[ERROR] Run a Dry Run first before Live Ingest." $RED
        return
    }
    $TxtLog.Clear()
    $script:StopRequested  = $false
    $script:Suffix = ""
    $script:ShootDescriptions = @{}
    $script:FileToShootMapping = @{}
    $BtnDryRun.Enabled     = $false
    SetLiveIngestEnabled $false
    SetVerifyEnabled $false
    $BtnStop.Enabled       = $true
    $DryRunBanner.Visible  = $IsDryRun
    if ($IsDryRun) {
        $BtnDryRun.Text      = "RUNNING..."
        $BtnDryRun.BackColor = $TEAL_DIM
    } else {
        $BtnLiveIngest.Text      = "RUNNING..."
        $BtnLiveIngest.BackColor = $TEAL_DIM
    }
    $LblStatus.ForeColor  = $YELLOW
    $ProgressBar.Value    = 0
    $LblPct.Text          = "0%"
    $LblFiles.Text        = ""
    $LblSpeed.Text        = ""
    $LblETA.Text          = ""
    $LblCurrentFile.Text  = ""

    $DEST_LIVE     = $DestBox.TextBox.Text.Trim()
    $LOG_DIR_LIVE  = $LogBox.TextBox.Text.Trim()
    $EXIFTOOL_LIVE = $ExifBox.TextBox.Text.Trim()

    if ($DEST_LIVE -eq "") {
        AppendLog "[ERROR] Vault destination path is empty." $RED; ResetUI; return
    }
    if (-not $IsDryRun -and -not (Test-Path $EXIFTOOL_LIVE)) {
        AppendLog "[ERROR] exiftool.exe not found at: $EXIFTOOL_LIVE" $RED
        AppendLog "        Update the ExifTool path in the Paths section above." $RED
        ResetUI; return
    }

    # Ignore the grey placeholder text - only a value the user (or detection) entered counts.
    $CopyrightVal = if ($TxtCopyright.Text -eq $TxtCopyright.Tag) { "" } else { $TxtCopyright.Text.Trim() }
    $metaParts = @()
    if ($CopyrightVal -ne "") { $metaParts += "-Copyright=`"$CopyrightVal`"" }
    $script:MetadataArgs = $metaParts -join " "

    $RawExtList   = ParseExtList $TxtRawExt.Text.Trim()
    $VideoExtList = ParseExtList $TxtVideoExt.Text.Trim()
    $AudioExtList = ParseExtList $TxtAudioExt.Text.Trim()
    $AuxRaw       = if ($TxtAuxExt.Text -eq $TxtAuxExt.Tag) { "" } else { $TxtAuxExt.Text.Trim() }
    $AuxExtList   = ParseExtList $AuxRaw

    if ($RawExtList.Count -eq 0) {
        AppendLog "[ERROR] No RAW extensions configured. Set at least one under FILE TYPES (e.g. nef, cr2, arw)." $RED
        ResetUI; return
    }

    $DriveLetter = $CmbDrive.SelectedItem.ToString()
    $SD_CARD     = $DriveLetter + "DCIM"

    if (-not (Test-Path $SD_CARD)) {
        AppendLog "[ERROR] SD card not found at $SD_CARD" $RED
        AppendLog "        Check the drive letter." $RED
        ResetUI; return
    }

    $LblStatus.Text = "Scanning files..."
    AppendLog "[INFO]  Scanning source files..." $YELLOW
    [System.Windows.Forms.Application]::DoEvents()

    AppendLog "[INFO]  • Scanning RAW files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $RawFiles   = GetFilesByExt $SD_CARD $RawExtList

    AppendLog "[INFO]  • Scanning video files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $VideoFiles = GetFilesByExt $SD_CARD $VideoExtList

    AppendLog "[INFO]  • Scanning audio files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $AudioFiles = GetFilesByExt $SD_CARD $AudioExtList

    AppendLog "[INFO]  • Scanning aux files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $AuxFiles   = GetFilesByExt $SD_CARD $AuxExtList

    AppendLog "[INFO]  • Scanning JPG files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $JpgFiles   = @(Get-ChildItem -Path $SD_CARD -Recurse -Include "*.jpg","*.jpeg" -ErrorAction SilentlyContinue)

    $TotalRawBytes   = ($RawFiles   | Measure-Object -Property Length -Sum).Sum
    $TotalVideoBytes = ($VideoFiles | Measure-Object -Property Length -Sum).Sum
    $TotalAudioBytes = ($AudioFiles | Measure-Object -Property Length -Sum).Sum
    $TotalAuxBytes   = ($AuxFiles   | Measure-Object -Property Length -Sum).Sum
    if (-not $TotalRawBytes)   { $TotalRawBytes   = 0 }
    if (-not $TotalVideoBytes) { $TotalVideoBytes = 0 }
    if (-not $TotalAudioBytes) { $TotalAudioBytes = 0 }
    if (-not $TotalAuxBytes)   { $TotalAuxBytes   = 0 }
    $TotalTransferBytes = $TotalRawBytes + $TotalVideoBytes + $TotalAudioBytes + $TotalAuxBytes
    $script:TotalFiles  = $RawFiles.Count + $VideoFiles.Count + $AudioFiles.Count + $AuxFiles.Count
    $script:TotalBytes  = $TotalTransferBytes
    $script:FilesDone   = 0
    $script:BytesDone   = 0

    AppendLog "[INFO]  Found $($RawFiles.Count) RAW files   ($(FormatBytes $TotalRawBytes))" $FG
    AppendLog "[INFO]  Found $($VideoFiles.Count) video files   ($(FormatBytes $TotalVideoBytes))" $FG
    if ($AudioExtList.Count -gt 0) {
        AppendLog "[INFO]  Found $($AudioFiles.Count) audio files ($($AudioExtList -join '/'))  ($(FormatBytes $TotalAudioBytes))" $FG
    }
    if ($AuxExtList.Count -gt 0) {
        AppendLog "[INFO]  Found $($AuxFiles.Count) aux files ($($AuxExtList -join '/'))  ($(FormatBytes $TotalAuxBytes))" $FG
    }
    AppendLog "[INFO]  Total: $(FormatBytes $TotalTransferBytes)" $FG

    GenerateThumbnailStrip ($RawFiles + $VideoFiles + $AuxFiles + $JpgFiles) $AudioFiles $EXIFTOOL_LIVE

    # Detect multiple shoots (separated by 30+ minutes)
    $LblStatus.Text = "Detecting multiple shoots..."
    AppendLog "`n[INFO]  Checking for multiple shoots..." $YELLOW
    [System.Windows.Forms.Application]::DoEvents()
    $detectedShoots = @(DetectShoots ($RawFiles + $VideoFiles + $JpgFiles) $EXIFTOOL_LIVE)
    $script:FileToShootMapping = @{}
    $useMultipleFolders = $false
    if ($detectedShoots.Count -gt 1) {
        AppendLog "[INFO]  Detected $($detectedShoots.Count) separate shoots." $TEAL
        foreach ($shoot in $detectedShoots) {
            foreach ($file in $shoot.Files) {
                $script:FileToShootMapping[$file.FullName] = $shoot.ShootIndex
            }
        }
        $useMultipleFolders = ShowShootSelectionDialog $detectedShoots $EXIFTOOL_LIVE
        if ($useMultipleFolders) {
            AppendLog "[INFO]  Multiple shoot folders will be created." $TEAL
            $script:MultipleShoots = $detectedShoots
        } else {
            AppendLog "[INFO]  All files will be merged into single folder." $FG_DIM
            $script:MultipleShoots = @()
        }
    } else {
        AppendLog "[INFO]  Single shoot detected." $FG_DIM
        $script:MultipleShoots = @()
    }

    if ($IsDryRun) {
        # Dry runs are just a preview - the description is only meaningful for
        # the real folder names created during a live ingest, so skip the
        # prompt and use blank placeholders that keep the indexing below intact.
        if ($useMultipleFolders -and $detectedShoots.Count -gt 1) {
            $jobDescriptions = @("") * $detectedShoots.Count
        } else {
            $jobDescriptions = @("")
        }
    } else {
        $jobDescriptions = @(ShowJobDescriptionDialog $detectedShoots.Count $useMultipleFolders)
        if ($jobDescriptions.Count -eq 0) {
            AppendLog "[INFO]  Ingest cancelled." $FG_DIM
            ResetUI; return
        }
    }

    if ($useMultipleFolders -and $detectedShoots.Count -gt 1) {
        $script:ShootDescriptions = @{}
        for ($i = 0; $i -lt $detectedShoots.Count; $i++) {
            $desc = $jobDescriptions[$i].Trim()
            $desc = $desc -replace '[\\/:*?"<>|]', '-'
            $script:ShootDescriptions[$detectedShoots[$i].ShootIndex] = $desc
        }
        $JobDescVal = ($script:ShootDescriptions.Values | Where-Object { $_ -ne "" }) -join ", "
    } else {
        $JobDescVal = $jobDescriptions[0].Trim()
        $script:Suffix = if ($JobDescVal -ne "") { " " + $JobDescVal } else { "" }
        $script:Suffix = $script:Suffix -replace '[\\/:*?"<>|]', '-'
    }

    $LblStatus.Text = "Building duplicate index..."
    AppendLog "[INFO]  Indexing vault for duplicates..." $YELLOW
    [System.Windows.Forms.Application]::DoEvents()

    AppendLog "[INFO]  • Scanning vault files..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $DestIndex = BuildDestIndex $DEST_LIVE
    AppendLog "[INFO]  • Found $($DestIndex.Count) existing files in vault" $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()

    AppendLog "[INFO]  • Checking for duplicates..." $FG_DIM
    [System.Windows.Forms.Application]::DoEvents()
    $dupCount = 0
    foreach ($f in ($RawFiles + $VideoFiles + $AudioFiles)) {
        if (IsDuplicateInVault $DestIndex $f.Name.ToLower() $f.Length $f.FullName) { $dupCount++ }
    }
    if ($dupCount -gt 0) {
        AppendLog "[WARN]  $dupCount file(s) already in vault - will be skipped." $ORANGE
    } else {
        AppendLog "[OK]    No duplicates detected." $GREEN
    }

    if (-not $IsDryRun) {
        $LblStatus.Text = "Checking storage..."
        $DestDrive   = Split-Path $DEST_LIVE -Qualifier
        $DriveInfo   = New-Object System.IO.DriveInfo($DestDrive)
        $FreeBytes   = $DriveInfo.AvailableFreeSpace
        $BufferBytes = 500MB
        AppendLog "[INFO]  Target drive $DestDrive free: $(FormatBytes $FreeBytes)" $FG
        if ($TotalTransferBytes -gt 0 -and ($TotalTransferBytes + $BufferBytes) -gt $FreeBytes) {
            $needed = FormatBytes ($TotalTransferBytes + $BufferBytes)
            $avail  = FormatBytes $FreeBytes
            AppendLog "[WARN]  Insufficient storage! Need $needed, have $avail." $RED
            $LblStatus.Text = "Insufficient storage!"
            $dlgResult = [System.Windows.Forms.MessageBox]::Show(
                "Not enough space on $DestDrive.`n`nRequired : $needed`nAvailable: $avail`n`nContinue anyway?",
                "VaultFlow - Storage Warning",
                [System.Windows.Forms.MessageBoxButtons]::OKCancel,
                [System.Windows.Forms.MessageBoxIcon]::Warning
            )
            if ($dlgResult -eq [System.Windows.Forms.DialogResult]::Cancel) {
                AppendLog "[INFO]  Cancelled (storage check)." $RED
                $LblStatus.Text = "Cancelled."; ResetUI; return
            }
            AppendLog "[WARN]  Continuing despite storage warning." $YELLOW
        } else {
            AppendLog "[OK]    Sufficient storage available." $GREEN
        }
    }

    New-Item -ItemType Directory -Force -Path $DEST_LIVE    | Out-Null
    New-Item -ItemType Directory -Force -Path $LOG_DIR_LIVE | Out-Null
    $Timestamp          = Get-Date -Format "yyyyMMdd_HHmmss"
    $LOG                = "$LOG_DIR_LIVE\ingest_$Timestamp.log"
    $script:IngestStart = [DateTime]::Now
    $modeLabel          = if ($IsDryRun) { "DRY RUN" } else { "LIVE" }

    AppendLog "`n============================================" $TEAL
    AppendLog " VAULTFLOW  [$modeLabel]  -  $(Get-Date -Format 'yyyy-MM-dd HH:mm')" $TEAL
    AppendLog " Source : $SD_CARD" $FG
    AppendLog " Dest   : $DEST_LIVE\{YYYY_MM}\{YYYY_MM_DD}$($script:Suffix)\RAW\{CAM_RAW|JPG|VIDEO|AUDIO|MISC}" $FG
    AppendLog "============================================" $TEAL

    foreach ($step in @(
        @{ Label="RAW files"; Files=$RawFiles; Bytes=$TotalRawBytes; Sub="CAM_RAW" },
        @{ Label="video files"; Files=$VideoFiles; Bytes=$TotalVideoBytes; Sub="VIDEO" },
        @{ Label="audio files"; Files=$AudioFiles; Bytes=$TotalAudioBytes; Sub="AUDIO" },
        @{ Label="aux files"; Files=$AuxFiles; Bytes=$TotalAuxBytes; Sub="MISC" }
    )) {
        if ($step.Files.Count -eq 0) {
            AppendLog "`n[INFO]  No $($step.Label) found - skipping." $FG_DIM
            continue
        }
        $LblStatus.Text = "Copying $($step.Label)..."
        AppendLog "`n[INFO]  Copying $($step.Files.Count) $($step.Label) ($(FormatBytes $step.Bytes))..." $YELLOW
        $ok = RunExifStep $step.Files $DestIndex $IsDryRun $DEST_LIVE $step.Sub
        if (-not $ok) { ResetUI; return }
        AppendLog "[OK]    $($step.Label) step complete." $GREEN
    }

    $LblStatus.Text = "Checking orphan JPGs..."
    AppendLog "`n[INFO]  Orphan JPG check..." $YELLOW
    $OrphanCount = 0
    foreach ($Jpg in $JpgFiles) {
        if ($script:StopRequested) { ResetUI; return }
        $BaseName = [System.IO.Path]::GetFileNameWithoutExtension($Jpg.Name)
        $RawMatch = $RawFiles | Where-Object { [System.IO.Path]::GetFileNameWithoutExtension($_.Name) -eq $BaseName } | Select-Object -First 1
        if (-not $RawMatch) {
            $fileKey = $Jpg.Name.ToLower()
            if (IsDuplicateInVault $DestIndex $fileKey $Jpg.Length $Jpg.FullName) {
                AppendLog "[SKIP]  Orphan JPG already in vault: $($Jpg.Name)" $ORANGE
            } else {
                AppendLog "[WARN]  Orphan JPG (no matching RAW file): $($Jpg.Name)" $YELLOW
                if (-not $IsDryRun) {
                    $fileSuffix = GetSuffixForFile $Jpg.FullName
                    $argStr = "-d `"$DEST_LIVE\%Y_%m\%Y_%m_%d$($fileSuffix)\RAW\JPG`" `"-Directory<DateTimeOriginal`" `"-Directory<CreateDate`" `"-Directory<FileModifyDate`" $($script:MetadataArgs) -o `".`" --overwrite_original `"$($Jpg.FullName)`""
                    $psi2 = New-Object System.Diagnostics.ProcessStartInfo
                    $psi2.FileName = $EXIFTOOL_LIVE; $psi2.Arguments = $argStr
                    $psi2.RedirectStandardOutput = $true; $psi2.UseShellExecute = $false; $psi2.CreateNoWindow = $true
                    $p2 = New-Object System.Diagnostics.Process; $p2.StartInfo = $psi2; $p2.Start() | Out-Null; $p2.WaitForExit()
                }
                $OrphanCount++
            }
        }
    }
    if ($OrphanCount -eq 0) { AppendLog "[OK]    No orphan JPGs." $GREEN }
    else { AppendLog "[WARN]  $OrphanCount orphan JPG(s) $(if ($IsDryRun) { 'would be copied' } else { 'copied' })." $YELLOW }

    # -- CLIENT FOLDER SCAFFOLD -----------------------------------
    # Ensures every day folder under the vault has the RAW/PX01/LR01
    # structure, not just the ones ExifTool happened to create while copying
    # RAW/video files. RAW itself is further split by media type
    # (CAM_RAW/JPG/VIDEO/AUDIO/MISC).
    $LblStatus.Text = "Building client folder structure..."
    if ($IsDryRun) {
        AppendLog "`n[INFO]  Would create client folder structure ($($ClientFolderTemplate -join ', ')) in touched day folder(s), with RAW split into $($RawTypeFolders -join ', ')." $FG_DIM
    } else {
        $ScaffoldCount = 0
        foreach ($MonthFolder in (Get-ChildItem -Path $DEST_LIVE -Directory -ErrorAction SilentlyContinue)) {
            $DayFolders = Get-ChildItem -Path $MonthFolder.FullName -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -match '^\d{4}_\d{2}_\d{2}' }
            foreach ($DayFolder in $DayFolders) {
                foreach ($SubFolder in $ClientFolderTemplate) {
                    New-Item -ItemType Directory -Force -Path (Join-Path $DayFolder.FullName $SubFolder) | Out-Null
                }
                foreach ($RawSubFolder in $RawTypeFolders) {
                    New-Item -ItemType Directory -Force -Path (Join-Path $DayFolder.FullName "RAW\$RawSubFolder") | Out-Null
                }
                $ScaffoldCount++
            }
        }
        if ($ScaffoldCount -gt 0) {
            AppendLog "`n[OK]    Client folder structure ensured in $ScaffoldCount day folder(s)." $GREEN
        }
    }

    $ProgressBar.Value   = 100
    $LblPct.Text         = "100%"
    $LblCurrentFile.Text = ""
    $LblStatus.Text      = "Verifying..."
    VerifyIngest ($RawFiles + $VideoFiles + $AudioFiles + $AuxFiles) $DEST_LIVE $IsDryRun $EXIFTOOL_LIVE

    $elapsed    = [DateTime]::Now - $script:IngestStart
    $elapsedStr = "{0}m {1}s" -f [int]$elapsed.TotalMinutes, $elapsed.Seconds
    $avgSpeed   = if (-not $IsDryRun -and $elapsed.TotalSeconds -gt 0) { FormatSpeed ($TotalTransferBytes / $elapsed.TotalSeconds) } else { "n/a (dry run)" }

    AppendLog "`n-- SUMMARY ---------------------" $TEAL
    AppendLog "  Mode        : $modeLabel" $(if ($IsDryRun) { $YELLOW } else { $GREEN })
    AppendLog "  RAW files   : $($RawFiles.Count)  ($(FormatBytes $TotalRawBytes))" $FG
    AppendLog "  Video files : $($VideoFiles.Count)  ($(FormatBytes $TotalVideoBytes))" $FG
    AppendLog "  Audio files : $($AudioFiles.Count)  ($(FormatBytes $TotalAudioBytes))" $FG
    AppendLog "  Aux files   : $($AuxFiles.Count)  ($(FormatBytes $TotalAuxBytes))" $FG
    AppendLog "  Duplicates  : $dupCount skipped" $(if ($dupCount -gt 0) { $ORANGE } else { $FG })
    AppendLog "  Orphan JPGs : $OrphanCount" $FG
    AppendLog "  Total size  : $(FormatBytes $TotalTransferBytes)" $FG
    AppendLog "  Avg speed   : $avgSpeed" $FG
    AppendLog "  Total time  : $elapsedStr" $FG
    AppendLog "  Log         : $LOG" $FG_DIM
    AppendLog "---------------------------------" $TEAL

    if ($script:EjectChecked -and -not $IsDryRun) {
        $LblStatus.Text = "Ejecting..."
        AppendLog "`n[INFO]  Ejecting $DriveLetter..." $YELLOW
        [System.Windows.Forms.Application]::DoEvents()
        try {
            $Shell  = New-Object -ComObject Shell.Application
            $Volume = $Shell.Namespace(17).ParseName($DriveLetter.TrimEnd("\"))
            if ($Volume) {
                $Volume.InvokeVerb("Eject")
                Start-Sleep -Milliseconds 500
                AppendLog "[OK]    SD card ejected." $GREEN
            } else { AppendLog "[WARN]  Remove SD card manually." $YELLOW }
        } catch { AppendLog "[WARN]  Eject error - remove manually." $YELLOW }
    }

    if ($IsDryRun) {
        # Pre-fill the Copyright box from the card's own tag so the live ingest
        # re-applies it - but never overwrite something already entered.
        $LblStatus.Text = "Detecting copyright..."
        [System.Windows.Forms.Application]::DoEvents()
        $detectedCopyright = DetectCopyright ($RawFiles + $JpgFiles + $VideoFiles) $EXIFTOOL_LIVE
        if ($detectedCopyright -eq $TxtCopyright.Tag) { $detectedCopyright = "" }   # the UI's placeholder text is never a real copyright
        if ($detectedCopyright -eq "") {
            AppendLog "`n[INFO]  No Copyright tag detected on the card files." $FG_DIM
        } elseif ($TxtCopyright.Text -eq $TxtCopyright.Tag -or $TxtCopyright.Text.Trim() -eq "") {
            $TxtCopyright.Text      = $detectedCopyright
            $TxtCopyright.ForeColor = $FG
            AppendLog "`n[INFO]  Detected copyright: $detectedCopyright  (filled in - edit before live ingest if wrong)" $TEAL
        } elseif ($TxtCopyright.Text.Trim() -ne $detectedCopyright) {
            AppendLog "`n[INFO]  Card copyright '$detectedCopyright' differs from the entered '$($TxtCopyright.Text.Trim())' - keeping what you entered." $FG_DIM
        }
        $script:DryRunCompleted = $true
        $LiveIngestToolTip.SetToolTip($BtnLiveIngest, "")
    } else {
        $script:VaultFolderReady = $true
        $script:LastIngestFolder = $DEST_LIVE
    }

    $finalMsg            = "$(if ($IsDryRun) { 'Dry run complete' } else { 'Done!' })  $elapsedStr  |  avg $avgSpeed"
    $LblStatus.ForeColor = $GREEN
    $LblETA.Text         = "Done in $elapsedStr"
    $LblSpeed.Text       = if (-not $IsDryRun) { "avg $avgSpeed" } else { "" }
    AppendLog "`nIngest $(if ($IsDryRun) { 'simulation ' } else { '' })complete." $TEAL
    ResetUI
    $LblStatus.Text      = $finalMsg
    $LblStatus.ForeColor = $GREEN

    if (-not $IsDryRun) {
        # Open the vault folder automatically and confirm with a popup so it's
        # obvious the ingest is done, even if the window isn't in focus.
        if (Test-Path $DEST_LIVE) {
            try { Start-Process "explorer.exe" -ArgumentList "`"$DEST_LIVE`"" } catch {}
        }
        $totalFilesDone = $RawFiles.Count + $VideoFiles.Count + $AudioFiles.Count + $AuxFiles.Count
        [System.Windows.Forms.MessageBox]::Show(
            "Ingest complete!`n`n$totalFilesDone files copied ($(FormatBytes $TotalTransferBytes)) in $elapsedStr.`n`nVault folder: $DEST_LIVE",
            "VaultFlow Ingest",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Information
        ) | Out-Null
    }
}

# ============================================================
#  VERIFY CARD vs VAULT  (read-only: size check, then SHA-256 of every match)
# ============================================================
function StartVerify {
    $TxtLog.Clear()
    $script:StopRequested = $false
    $BtnDryRun.Enabled = $false
    SetLiveIngestEnabled $false
    SetVerifyEnabled $false
    $BtnStop.Enabled = $true
    $LblStatus.ForeColor = $YELLOW
    $ProgressBar.Value = 0
    $LblPct.Text = "0%"
    $LblFiles.Text = ""; $LblSpeed.Text = ""; $LblETA.Text = ""; $LblCurrentFile.Text = ""

    $DEST_LIVE     = $DestBox.TextBox.Text.Trim()
    $EXIFTOOL_LIVE = $ExifBox.TextBox.Text.Trim()
    $SD_CARD       = $CmbDrive.SelectedItem.ToString() + "DCIM"
    if (-not (Test-Path $SD_CARD)) {
        AppendLog "[ERROR] SD card not found at $SD_CARD - verification needs the card inserted (not ejected)." $RED
        ResetUI; return
    }
    if ($DEST_LIVE -eq "" -or -not (Test-Path $DEST_LIVE)) {
        AppendLog "[ERROR] Vault destination not found: $DEST_LIVE" $RED
        ResetUI; return
    }

    $AuxRaw = if ($TxtAuxExt.Text -eq $TxtAuxExt.Tag) { "" } else { $TxtAuxExt.Text.Trim() }
    $LblStatus.Text = "Scanning..."
    AppendLog "[INFO]  Scanning card and vault..." $YELLOW
    [System.Windows.Forms.Application]::DoEvents()
    $srcFiles = @(GetFilesByExt $SD_CARD (ParseExtList $TxtRawExt.Text.Trim())) +
                @(GetFilesByExt $SD_CARD (ParseExtList $TxtVideoExt.Text.Trim())) +
                @(GetFilesByExt $SD_CARD (ParseExtList $TxtAudioExt.Text.Trim())) +
                @(GetFilesByExt $SD_CARD (ParseExtList $AuxRaw))
    if ($srcFiles.Count -eq 0) {
        AppendLog "[WARN]  No files matching the configured extensions found on the card." $YELLOW
        ResetUI; return
    }
    $vaultIndex = BuildVaultNameIndex $DEST_LIVE
    AppendLog "[INFO]  Card: $($srcFiles.Count) files ($(FormatBytes (($srcFiles | Measure-Object Length -Sum).Sum)))   Vault: $($vaultIndex.Count) distinct file names" $FG

    # Phase 1 - presence and size (fast)
    $LblStatus.Text = "Checking sizes..."
    AppendLog "`n[INFO]  Phase 1/2: presence and size..." $YELLOW
    $results = @{ "MISSING" = @(); "SIZE" = @(); "HASH" = @(); "OK-TAGGED" = @() }   # status -> file names
    $toHash  = @()
    $i = 0
    foreach ($f in $srcFiles) {
        if ($script:StopRequested) { AppendLog "[WARN]  Verification stopped." $RED; return }
        $i++
        $LblCurrentFile.Text = "Checking: $($f.Name)"
        $LblFiles.Text = "$i / $($srcFiles.Count) files"
        $status = VerifyFilePair $f $vaultIndex[$f.Name.ToLower()] $EXIFTOOL_LIVE $false
        if ($status -eq "STOPPED") { AppendLog "[WARN]  Verification stopped." $RED; return }
        if ($status -eq "OK") { $toHash += $f }
        else {
            if (-not $results.ContainsKey($status)) { $results[$status] = @() }
            $results[$status] += $f.Name
        }
        [System.Windows.Forms.Application]::DoEvents()
    }
    AppendLog ("  Size OK: $($toHash.Count)   Tagged (image data identical): $(@($results['OK-TAGGED']).Count)   Missing: $(@($results['MISSING']).Count)   Size mismatch: $(@($results['SIZE']).Count)") $FG

    # Phase 2 - SHA-256 of card file vs vault file for every size match
    $script:VerifyBytesTotal = 2 * (($toHash | Measure-Object Length -Sum).Sum)
    $script:VerifyBytesDone  = 0
    $hashOk = 0
    $i = 0
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $LblStatus.Text = "Hashing..."
    AppendLog "`n[INFO]  Phase 2/2: SHA-256 of $($toHash.Count) size-matched files (this reads both copies in full)..." $YELLOW
    foreach ($f in $toHash) {
        if ($script:StopRequested) { AppendLog "[WARN]  Verification stopped." $RED; return }
        $i++
        $LblCurrentFile.Text = "Hashing: $($f.Name)"
        $LblFiles.Text = "$i / $($toHash.Count) files"
        $status = VerifyFilePair $f $vaultIndex[$f.Name.ToLower()] $EXIFTOOL_LIVE $true
        if ($status -eq "STOPPED") { AppendLog "[WARN]  Verification stopped." $RED; return }
        if ($status -eq "OK") { $hashOk++ }
        else {
            if (-not $results.ContainsKey($status)) { $results[$status] = @() }
            $results[$status] += $f.Name
        }
        if ($clock.Elapsed.TotalSeconds -gt 1 -and $script:VerifyBytesTotal -gt 0) {
            $speed = $script:VerifyBytesDone / $clock.Elapsed.TotalSeconds
            $LblSpeed.Text = FormatSpeed $speed
            $LblETA.Text   = "ETA  " + (FormatETA (($script:VerifyBytesTotal - $script:VerifyBytesDone) / $speed))
        }
        [System.Windows.Forms.Application]::DoEvents()
    }
    $ProgressBar.Value = 100; $LblPct.Text = "100%"; $LblCurrentFile.Text = ""

    $tagged  = @($results["OK-TAGGED"]).Count
    $problem = @($results["MISSING"]).Count + @($results["SIZE"]).Count + @($results["HASH"]).Count
    AppendLog "`n-- VERIFY SUMMARY --------------" $TEAL
    AppendLog "  Card files        : $($srcFiles.Count)" $FG
    AppendLog "  Hash verified     : $hashOk" $FG
    AppendLog "  Tagged, data OK   : $tagged  (bytes differ only because Copyright metadata was written)" $FG
    AppendLog "  Missing           : $(@($results['MISSING']).Count)" $(if (@($results['MISSING']).Count) { $RED } else { $FG })
    AppendLog "  Size mismatch     : $(@($results['SIZE']).Count)" $(if (@($results['SIZE']).Count) { $RED } else { $FG })
    AppendLog "  Hash mismatch     : $(@($results['HASH']).Count)" $(if (@($results['HASH']).Count) { $RED } else { $FG })
    foreach ($k in @("MISSING", "SIZE", "HASH")) {
        foreach ($n in @($results[$k])) { if ($n) { AppendLog "    ${k}: $n" $RED } }
    }
    AppendLog "--------------------------------" $TEAL
    if ($problem -eq 0) {
        AppendLog "[OK]    All $($srcFiles.Count) card files are intact in the vault." $GREEN
        AppendLog "[OK]    Safe to format SD card." $GREEN
        $LblStatus.ForeColor = $GREEN
        $LblStatus.Text = "Verified OK."
    } else {
        AppendLog "[WARN]  $problem file(s) failed verification. Do NOT format the SD card - re-run Live Ingest (it skips intact duplicates)." $RED
        $LblStatus.ForeColor = $RED
        $LblStatus.Text = "$problem file(s) failed."
    }
    $savedText = $LblStatus.Text; $savedColor = $LblStatus.ForeColor
    ResetUI
    $LblStatus.Text = $savedText; $LblStatus.ForeColor = $savedColor
}

$BtnVerify.Add_Click({ StartVerify })
$BtnDryRun.Add_Click({ StartIngest $true })
$BtnLiveIngest.Add_Click({ StartIngest $false })

try {
    [System.Windows.Forms.Application]::Run($Form)
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "Startup error:`n`n$($_.Exception.Message)`n`nLine: $($_.InvocationInfo.ScriptLineNumber)",
        "VaultFlow - Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    )
}
