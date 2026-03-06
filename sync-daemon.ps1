param(
    [string]$Source = "",
    [string]$Destination = ""
)

if (-not $Source) {
    $Source = Join-Path $PSScriptRoot "reaper_daemon.lua"
}

if (-not $Destination) {
    $Destination = Join-Path $env:APPDATA "REAPER\Scripts\reaper_daemon.lua"
}

if (-not (Test-Path $Source)) {
    Write-Error "Source not found: $Source"
    exit 1
}

$destDir = Split-Path -Path $Destination -Parent
if (-not (Test-Path $destDir)) {
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null
}

Copy-Item -Path $Source -Destination $Destination -Force
Write-Output "Synced daemon: $Source -> $Destination"
