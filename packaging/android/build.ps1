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

# The build now emits one APK per ABI (see the splits block in
# app/build.gradle.kts), so name the ABI on every line. Two files called
# app-release.apk leave a sideloading user with no way to tell which one their
# phone can actually run -- a worse outcome than the single fat APK was.
$apkDir = Join-Path $PSScriptRoot 'app\build\outputs\apk'
if (-not (Test-Path $apkDir)) { throw "Gradle succeeded but $apkDir does not exist" }

$apks = @(Get-ChildItem -Path $apkDir -Recurse -Filter *.apk)
if ($apks.Count -eq 0) { throw "Gradle succeeded but produced no APK under $apkDir" }

foreach ($apk in $apks) {
    # Flavour names, so these match app-arm64-release.apk / app-x86_64-release.apk.
    # x86_64 is tested before arm64 because neither is a substring of the other,
    # but the order still documents which one a sideloading user wants.
    $abi = switch -Regex ($apk.Name) {
        'x86_64' { 'x86_64  (emulator)'; break }
        'arm64'  { 'arm64-v8a  (phone)'; break }
        default  { 'ABI NOT IN NAME'; break }
    }
    "APK [{0,-20}] {1}  ({2} MB)" -f $abi, $apk.FullName, [math]::Round($apk.Length / 1MB, 1)
}
