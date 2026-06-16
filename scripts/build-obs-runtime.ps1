# SPDX-License-Identifier: GPL-3.0-or-later
# Assemble the bundled OBS runtime that AutoClip's Windows recorder needs:
# a curated libobs + embeddable Python, laid out so OBS resolves its helper exes
# (obs-ffmpeg-mux.exe / obs-nvenc-test.exe) and core data via the host-exe directory.
#
# Produces <repo>/obs-runtime/ (~80 MB). Requires an OBS Studio install to copy from
# (winget install OBSProject.OBSStudio) and downloads the Python embeddable zip.
#
# Usage:  pwsh scripts/build-obs-runtime.ps1 [-ObsDir "C:\Program Files\obs-studio"]
[CmdletBinding()]
param(
    [string]$ObsDir = "C:\Program Files\obs-studio",
    [string]$PyVersion = "3.11.9",
    [string]$OutDir = (Join-Path (Split-Path $PSScriptRoot -Parent) "obs-runtime")
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path "$ObsDir\bin\64bit\obs.dll")) {
    throw "OBS not found at $ObsDir. Install with: winget install OBSProject.OBSStudio"
}

$bin = Join-Path $OutDir "bin\64bit"
if (Test-Path $OutDir) { Remove-Item $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $bin -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutDir "obs-plugins\64bit") -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $OutDir "data\obs-plugins") -Force | Out-Null

# 1) embeddable Python -> bin\64bit (runs the recorder; self-contained stdlib + ctypes)
$pyZip = Join-Path $env:TEMP "python-$PyVersion-embed-amd64.zip"
if (-not (Test-Path $pyZip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip" -OutFile $pyZip -UseBasicParsing
}
Expand-Archive -Path $pyZip -DestinationPath $bin -Force

# 2) curated OBS bin -> bin\64bit (exclude the GUI/Qt/browser bloat the recorder never loads)
$exclude = @('obs64.exe','obs64.pdb','obs.pdb','Qt6Gui.dll','Qt6Widgets.dll','Qt6Core.dll',
             'Qt6Network.dll','Qt6Svg.dll','Qt6Qml.dll','datachannel.dll')
Get-ChildItem "$ObsDir\bin\64bit" -File | Where-Object { $exclude -notcontains $_.Name } |
    ForEach-Object { Copy-Item $_.FullName $bin -Force }

# 3) only the plugins the recorder uses, plus their data
$mods = 'win-capture','win-wasapi','obs-ffmpeg','obs-nvenc','obs-x264'
foreach ($m in $mods) {
    Copy-Item "$ObsDir\obs-plugins\64bit\$m.dll" (Join-Path $OutDir "obs-plugins\64bit")
    $md = "$ObsDir\data\obs-plugins\$m"
    if (Test-Path $md) { Copy-Item $md (Join-Path $OutDir "data\obs-plugins") -Recurse -Force }
}
Copy-Item "$ObsDir\data\libobs" (Join-Path $OutDir "data") -Recurse -Force

# 5) the recorder script itself (self-contained: stdlib + ctypes), so the bundled
#    OBS python can run it by path even when the GUI is frozen into an exe.
$recSrc = Join-Path (Split-Path $PSScriptRoot -Parent) "autoclip\core\obs_recorder.py"
Copy-Item $recSrc (Join-Path $OutDir "obs_recorder.py") -Force

$mb = [math]::Round((Get-ChildItem $OutDir -Recurse -File | Measure-Object Length -Sum).Sum/1MB, 1)
Write-Host "Built $OutDir ($mb MB)"
