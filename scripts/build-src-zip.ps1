# SPDX-License-Identifier: GPL-3.0-or-later
# Package the loose `autoclip/` source tree for a code-only update (Option A).
# Produces release/autoclip-src-<version>-rt<runtime>.zip with autoclip/ at the root.
# Attach this zip to a GitHub release; installed Windows apps with a compatible runtime
# will download it and replace their loose autoclip/ in place (no installer needed).
#
# Bump RUNTIME_VERSION in autoclip_app.py ONLY when the frozen runtime / deps change;
# then a fresh installer must ship and older installs will fall back to it automatically.
#
# Usage:  pwsh scripts/build-src-zip.ps1
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent

# Read __version__ and RUNTIME_VERSION from source so the name can't drift.
$verLine = Select-String -Path (Join-Path $repo "autoclip\__init__.py") -Pattern '__version__\s*=\s*"([^"]+)"'
$version = $verLine.Matches[0].Groups[1].Value
$rtLine  = Select-String -Path (Join-Path $repo "autoclip_app.py") -Pattern 'RUNTIME_VERSION\s*=\s*(\d+)'
$runtime = $rtLine.Matches[0].Groups[1].Value

$releaseDir = Join-Path $repo "release"
New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
$out = Join-Path $releaseDir "autoclip-src-$version-rt$runtime.zip"
if (Test-Path $out) { Remove-Item $out -Force }

# Stage a clean copy of autoclip/ (no caches) so the zip has autoclip/ at its root.
$stage = Join-Path $env:TEMP "autoclip-src-stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
Copy-Item (Join-Path $repo "autoclip") (Join-Path $stage "autoclip") -Recurse
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem $stage -Recurse -Include *.pyc,*.pyo | Remove-Item -Force

Compress-Archive -Path (Join-Path $stage "autoclip") -DestinationPath $out
Remove-Item $stage -Recurse -Force

$mb = [math]::Round((Get-Item $out).Length/1MB, 2)
Write-Host "Built $out ($mb MB)  [version=$version runtime=$runtime]"
