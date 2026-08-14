<#
.SYNOPSIS
  Remove everything install.ps1 created. Leaves generated briefs and logs alone
  unless you pass -Purge.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'Daily Brief',
    [switch]$Purge
)

$ErrorActionPreference = 'Continue'
$base = $PSScriptRoot

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'"
}
else { Write-Host "No scheduled task '$TaskName' found" }

foreach ($lnk in @((Join-Path ([Environment]::GetFolderPath('Programs')) 'Daily Brief.lnk'),
                   (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Daily Brief.lnk'))) {
    if (Test-Path $lnk) { Remove-Item $lnk -Force; Write-Host "Removed shortcut $lnk" }
}

foreach ($key in @('HKCU:\Software\Classes\AppUserModelId\Local.DailyBrief',
                   'HKCU:\Software\Classes\dailybrief')) {
    if (Test-Path $key) {
        Remove-Item -Path $key -Recurse -Force
        Write-Host "Removed $key"
    }
}

if ($Purge) {
    foreach ($d in @('briefs', 'logs', 'workspace')) {
        $p = Join-Path $base $d
        if (Test-Path $p) { Remove-Item $p -Recurse -Force; Write-Host "Purged $p" }
    }
    foreach ($f in @('state.json', 'icon.png')) {
        $p = Join-Path $base $f
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "Purged $p" }
    }
}
else {
    Write-Host "Briefs and logs left in place (pass -Purge to delete them)."
}

Write-Host "Done. The program files themselves are still in $base - delete the folder to finish."
