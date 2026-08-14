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

    # The spec's COLLECT builds dist\windows\DailyBrief\ -- both exes plus
    # _internal\. Everything below goes INTO that folder, not beside it:
    # app_home() is the executable's parent directory, so that folder is where
    # the app will look for config.json and where it will write briefs\ and
    # logs\. It is also the folder to zip for the release.
    $app = Join-Path $out 'DailyBrief'
    if (-not (Test-Path $app)) { throw "PyInstaller did not produce $app" }

    # Ship the examples so a fresh install has something to copy from. The live
    # config.json and calendars.txt are deliberately NOT bundled: calendars.txt
    # holds a Google secret iCal URL, which is a permanent bearer token.
    Copy-Item (Join-Path $root 'config.example.json')    $app -Force
    Copy-Item (Join-Path $root 'calendars.example.txt')  $app -Force
    Copy-Item (Join-Path $root 'README.md')              $app -Force

    $readme = @'
Daily Brief for Windows
=======================

  DailyBrief.exe    the command line: run, status, check, sources, setup
  DailyBriefw.exe   the same thing without a console window, for the 08:00 task
  _internal\        the Python runtime that both exes need

KEEP THIS FOLDER TOGETHER. The exes do not contain the Python runtime -- it is
in _internal\ beside them. Copy DailyBrief.exe out on its own and you get an
exe that will not start. Moving or renaming the whole folder is fine, and the
install moves with it.

First run:
  1. Copy config.example.json to config.json and set your city.
  2. Optionally copy calendars.example.txt to calendars.txt and add a calendar.
  3. DailyBrief.exe run --open

Everything the app writes -- config.json, briefs\, logs\, state.json -- lives
in this folder, beside the exe. To put it somewhere else, set DAILYBRIEF_HOME.

To schedule the daily brief, point a Task Scheduler action at DailyBriefw.exe
with the argument: run
'@
    Set-Content -Path (Join-Path $app 'READ-ME-FIRST.txt') -Value $readme -Encoding utf8

    Write-Host ''
    Write-Host "  $app"
    # _internal\ is a directory and most of the download now lives in it, so sum
    # it rather than print a blank where the biggest number should be.
    Get-ChildItem $app | ForEach-Object {
        $bytes = if ($_.PSIsContainer) {
            (Get-ChildItem $_.FullName -Recurse -File | Measure-Object -Property Length -Sum).Sum
        } else { $_.Length }
        "  {0,-24} {1,8:N1} MB" -f $_.Name, ($bytes / 1MB)
    }
} finally {
    Pop-Location
}
