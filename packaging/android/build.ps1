# Builds the Daily Brief Android APK.
#   powershell -ExecutionPolicy Bypass -File packaging\android\build.ps1 [assembleDebug|assembleRelease]

param([string]$Task = "assembleDebug")

$ErrorActionPreference = 'Stop'
$env:JAVA_HOME = 'C:\Users\Danny\Programs\jdk-17'
$env:ANDROID_HOME = 'C:\Users\Danny\Programs\android-sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"

& 'C:\Users\Danny\Programs\gradle-8.11.1\bin\gradle.bat' `
    -p $PSScriptRoot --no-daemon --console=plain $Task

if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Get-ChildItem -Path "$PSScriptRoot\app\build\outputs\apk" -Recurse -Filter *.apk -ErrorAction SilentlyContinue |
    ForEach-Object { "APK: $($_.FullName)  ($([math]::Round($_.Length/1MB,1)) MB)" }
