<#
.SYNOPSIS
  Fire a Windows toast notification for the Daily Brief.

.DESCRIPTION
  Uses the WinRT ToastNotificationManager directly, so no third-party module is
  required. Clicking the toast activates -Launch via protocol activation (the
  `dailybrief:` handler that install.ps1 registers).

  Writes TOAST_OK or TOAST_FAILED to stdout so the Python caller can tell.
#>
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Body,
    [string]$Launch = '',
    [string]$Aumid = 'Local.DailyBrief',
    [string]$Attribution = '',
    [string]$Icon = ''
)

$ErrorActionPreference = 'Stop'

# The always-present shell AUMID, used if our own identity is not registered.
$FallbackAumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }

try {
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

    if (-not $Icon) {
        $maybe = Join-Path $PSScriptRoot 'icon.png'
        if (Test-Path $maybe) { $Icon = $maybe }
    }

    $launchAttr = ''
    if ($Launch) { $launchAttr = ' launch="' + (Esc $Launch) + '" activationType="protocol"' }

    $imageEl = ''
    if ($Icon -and (Test-Path $Icon)) {
        $uri = 'file:///' + ((Resolve-Path $Icon).Path -replace '\\', '/')
        $imageEl = '<image placement="appLogoOverride" hint-crop="circle" src="' + (Esc $uri) + '"/>'
    }

    $attrEl = ''
    if ($Attribution) { $attrEl = '<text placement="attribution">' + (Esc $Attribution) + '</text>' }

    $xmlText = '<toast' + $launchAttr + ' duration="long">' +
               '<visual><binding template="ToastGeneric">' +
               '<text>' + (Esc $Title) + '</text>' +
               '<text>' + (Esc $Body) + '</text>' +
               $imageEl + $attrEl +
               '</binding></visual>' +
               '<audio src="ms-winsoundevent:Notification.Default"/>' +
               '</toast>'

    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml($xmlText)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $xml
    $toast.Tag = 'dailybrief'
    $toast.Group = 'dailybrief'

    # Prefer our registered identity; fall back to the shell's if it is missing,
    # so a toast still appears on a machine where install.ps1 has not been run.
    $used = $Aumid
    try {
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($Aumid).Show($toast)
    }
    catch {
        $used = $FallbackAumid
        $toast2 = New-Object Windows.UI.Notifications.ToastNotification $xml
        $toast2.Tag = 'dailybrief'
        $toast2.Group = 'dailybrief'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($FallbackAumid).Show($toast2)
    }

    Write-Output "TOAST_OK ($used)"
    exit 0
}
catch {
    Write-Output "TOAST_FAILED: $($_.Exception.Message)"
    exit 1
}
