# SPDX-License-Identifier: GPL-3.0-or-later
# Package a light code-only update payload (Option A).
#
# The zip MIRRORS the install layout — files sit at their real install-relative
# paths — and the updater simply overlays the tree onto the install folder. So
# adding a new loose file to a future update is just: include it here at its path.
# No updater change needed. (The big obs-runtime binaries and the frozen runtime
# are NOT here — those only change via the full installer.)
#
# Currently shipped: autoclip/ (the app) + the loose obs-runtime helper scripts.
# Produces release/autoclip-src-<version>-rt<runtime>.zip. Attach to a GitHub release.
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

# Stage a tree that mirrors the install layout.
$stage = Join-Path $env:TEMP "autoclip-update-stage"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

# autoclip/ (the app package, no caches)
Copy-Item (Join-Path $repo "autoclip") (Join-Path $stage "autoclip") -Recurse
Get-ChildItem $stage -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem $stage -Recurse -Include *.pyc,*.pyo | Remove-Item -Force

# obs-runtime/ — only the loose helper scripts (run by the embeddable python;
# their source is autoclip/core/). Keep this list in sync with build-obs-runtime.ps1.
$obsStage = Join-Path $stage "obs-runtime"
New-Item -ItemType Directory -Force -Path $obsStage | Out-Null
foreach ($h in "obs_recorder.py", "obs_audio_capture.py") {
    Copy-Item (Join-Path $repo "autoclip\core\$h") (Join-Path $obsStage $h)
}

# Zip the staged top-level entries so the archive root = the install-relative tree.
Compress-Archive -Path (Get-ChildItem $stage | ForEach-Object { $_.FullName }) -DestinationPath $out
Remove-Item $stage -Recurse -Force

$mb = [math]::Round((Get-Item $out).Length/1MB, 2)
Write-Host "Built $out ($mb MB)  [version=$version runtime=$runtime]"
