<#
.SYNOPSIS
  Install the Daily Brief: scheduled task, toast identity, and click handler.

.DESCRIPTION
  Everything this touches is user-scope (HKCU + a per-user scheduled task), so
  it needs no administrator rights and uninstall.ps1 removes all of it.

    1. HKCU:\Software\Classes\AppUserModelId\Local.DailyBrief
       Gives the toast a proper name and icon instead of "Windows PowerShell".
    2. HKCU:\Software\Classes\dailybrief
       A `dailybrief:` URL handler, so clicking the toast opens the brief.
    3. Scheduled Task "Daily Brief"
       Runs daily at -Time. -StartWhenAvailable means that if the PC was off,
       it runs shortly after you next log on instead of skipping the day.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Time 07:30
#>
[CmdletBinding()]
param(
    [string]$Time = '08:00',
    [string]$TaskName = 'Daily Brief',
    [string]$Pythonw = '',
    [switch]$SkipTask,
    [switch]$Desktop
)

$ErrorActionPreference = 'Stop'
$base = $PSScriptRoot
$script = Join-Path $base 'dailybrief.py'
$aumid = 'Local.DailyBrief'

if (-not (Test-Path $script)) { throw "dailybrief.py not found next to install.ps1 ($base)" }

# --- locate pythonw.exe (windowless, so no console flashes at 08:00) ---------
if (-not $Pythonw) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) {
        $cand = Join-Path (Split-Path $py.Source -Parent) 'pythonw.exe'
        if (Test-Path $cand) { $Pythonw = $cand } else { $Pythonw = $py.Source }
    }
}
if (-not $Pythonw -or -not (Test-Path $Pythonw)) {
    throw "Could not find pythonw.exe. Pass one explicitly: -Pythonw 'C:\Path\pythonw.exe'"
}
Write-Host "Python      : $Pythonw"

# --- icon -------------------------------------------------------------------
$iconPng = Join-Path $base 'icon.png'
$iconIco = Join-Path $base 'icon.ico'
& $Pythonw $script icon | Out-Null
if (-not (Test-Path $iconPng)) { Write-Warning "icon.png was not generated; toasts will use a default icon." }

# --- Start Menu / desktop shortcuts -----------------------------------------
# The brief opens in a chromeless window with no address bar, so without a
# shortcut the only ways in are the toast and a terminal command.
function New-BriefShortcut([string]$LinkPath) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($LinkPath)
    $lnk.TargetPath = $Pythonw
    $lnk.Arguments = ('"{0}" open' -f $script)
    $lnk.WorkingDirectory = $base
    $lnk.Description = "Open today's daily brief"
    if (Test-Path $iconIco) { $lnk.IconLocation = "$iconIco,0" }
    $lnk.Save()
    [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
}

$startMenu = Join-Path ([Environment]::GetFolderPath('Programs')) 'Daily Brief.lnk'
New-BriefShortcut $startMenu
Write-Host "Shortcut    : Start Menu -> 'Daily Brief'"

if ($Desktop) {
    $desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Daily Brief.lnk'
    New-BriefShortcut $desktopLnk
    Write-Host "Shortcut    : Desktop -> 'Daily Brief'"
}

# --- 1. toast app identity --------------------------------------------------
$appKey = "HKCU:\Software\Classes\AppUserModelId\$aumid"
if (-not (Test-Path $appKey)) { New-Item -Path $appKey -Force | Out-Null }
New-ItemProperty -Path $appKey -Name 'DisplayName' -Value 'Daily Brief' -PropertyType String -Force | Out-Null
if (Test-Path $iconPng) {
    New-ItemProperty -Path $appKey -Name 'IconUri' -Value $iconPng -PropertyType String -Force | Out-Null
}
New-ItemProperty -Path $appKey -Name 'ShowInSettings' -Value 1 -PropertyType DWord -Force | Out-Null
Write-Host "Toast id    : $aumid  (registered)"

# --- 2. dailybrief: protocol handler ---------------------------------------
$protoKey = 'HKCU:\Software\Classes\dailybrief'
$cmdKey = Join-Path $protoKey 'shell\open\command'
if (-not (Test-Path $cmdKey)) { New-Item -Path $cmdKey -Force | Out-Null }
Set-ItemProperty -Path $protoKey -Name '(Default)' -Value 'URL:Daily Brief' -Force
New-ItemProperty -Path $protoKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
# %1 carries the full dailybrief:<verb> URL, so one handler serves both the
# toast click (open) and the in-page Refresh button (refresh).
Set-ItemProperty -Path $cmdKey -Name '(Default)' -Value ('"{0}" "{1}" protocol "%1"' -f $Pythonw, $script) -Force
Write-Host "Click opens : dailybrief:<verb> -> $script protocol"

# --- 3. scheduled task ------------------------------------------------------
if ($SkipTask) {
    Write-Host "Task        : skipped (-SkipTask)"
}
else {
    try { $at = [datetime]::ParseExact($Time, 'HH:mm', $null) }
    catch { throw "-Time must be 24-hour HH:mm, e.g. 07:30 (got '$Time')" }

    # No PYTHONIOENCODING is set here: a Scheduled Task action cannot carry env
    # vars without wrapping it in cmd.exe (which flashes a console). dailybrief.py
    # reconfigures its own streams to UTF-8 in main() instead, which also covers
    # pythonw's stdout being None entirely.
    $action = New-ScheduledTaskAction -Execute $Pythonw -Argument ('"{0}" run' -f $script) -WorkingDirectory $base
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -MultipleInstances IgnoreNew -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
        -Principal $principal -Description 'Generates the daily brief with Claude Code and shows a toast.' `
        -Force | Out-Null

    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host "Task        : '$TaskName' daily at $Time (next run $($info.NextRunTime))"
    Write-Host "              Missed runs (PC off) fire shortly after your next logon."
}

Write-Host ''
Write-Host 'Installed. Useful commands:' -ForegroundColor Green
Write-Host "  python `"$script`" run --force --open   # generate one right now"
Write-Host "  python `"$script`" status              # config + last run"
Write-Host "  python `"$script`" open                # reopen the latest brief"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'   # test the scheduled path"
