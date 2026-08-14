# Builds the Daily Brief Windows release into dist\windows\.
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
$out = Join-Path $root 'dist\windows'
$work = Join-Path $root 'build\pyinstaller'

Push-Location $root
try {
    if (-not (Test-Path (Join-Path $root 'icon.ico'))) {
        Write-Host 'icon.ico missing; generating it first...'
        python dailybrief.py icon | Out-Null
    }

    Write-Host 'Running PyInstaller...'
    python -m PyInstaller `
        --noconfirm `
        --distpath $out `
        --workpath $work `
        (Join-Path $root 'packaging\windows\dailybrief.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller exited $LASTEXITCODE" }

    # Ship the examples so a fresh install has something to copy from. The live
    # config.json and calendars.txt are deliberately NOT bundled: calendars.txt
    # holds a Google secret iCal URL, which is a permanent bearer token.
    Copy-Item (Join-Path $root 'config.example.json')    $out -Force
    Copy-Item (Join-Path $root 'calendars.example.txt')  $out -Force
    Copy-Item (Join-Path $root 'README.md')              $out -Force

    $readme = @'
Daily Brief for Windows
=======================

  DailyBrief.exe    the command line: run, status, check, sources, setup
  DailyBriefw.exe   the same thing without a console window, for the 08:00 task

First run:
  1. Copy config.example.json to config.json and set your city.
  2. Optionally copy calendars.example.txt to calendars.txt and add a calendar.
  3. DailyBrief.exe run --open

Everything the app writes -- config.json, briefs\, logs\, state.json -- lives
in this folder, beside the exe. Move the folder and the whole install moves
with it. To put it somewhere else, set DAILYBRIEF_HOME.

To schedule the daily brief, point a Task Scheduler action at DailyBriefw.exe
with the argument: run
'@
    Set-Content -Path (Join-Path $out 'READ-ME-FIRST.txt') -Value $readme -Encoding utf8

    Write-Host ''
    Get-ChildItem $out | ForEach-Object {
        "  {0,-24} {1,8:N1} MB" -f $_.Name, ($_.Length / 1MB)
    }
} finally {
    Pop-Location
}
