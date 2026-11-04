<#
  Builds the PixelPaw OBS plugin on Windows (x64) and optionally installs it.

    powershell -ExecutionPolicy Bypass -File obs\plugin\build.ps1            # build -> obs\plugin\dist\pixelpaw
    powershell -ExecutionPolicy Bypass -File obs\plugin\build.ps1 -Install   # build + install for OBS (close OBS first)

  Needs Visual Studio 2022+ (or Build Tools) with "Desktop development with C++" (includes CMake + Ninja).
  Downloads once into obs\plugin\.deps (checked with SHA-256): OBS headers and the Qt SDK that OBS itself is built with.
  Import libraries come from your installed OBS, so the plugin matches it exactly.
#>
param([switch]$Install, [switch]$Clean)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Root = $PSScriptRoot
$Deps = Join-Path $Root '.deps'
$Sdk = Join-Path $Deps 'sdk'
$Build = Join-Path $Root 'build'
$Dist = Join-Path $Root 'dist\pixelpaw'

# Pinned to OBS 33.0.1 (its CMakePresets.json "dependencies" block).
$ObsTag = '33.0.1'
$DepsVersion = '2026-10-23'
$QtSha256 = '78cd7a138464bb6edcdd3b85fcd63a5a7c69e369629963f3bc395bab74fb3f47'
$BrowserCommit = 'd4db73e7e5d5b51d853177c8cddc796715a8ac84'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# ---- OBS install ---------------------------------------------------------------------------
$obsDir = $null
foreach ($key in 'HKLM:\SOFTWARE\OBS Studio', 'HKLM:\SOFTWARE\WOW6432Node\OBS Studio') {
  try { $obsDir = (Get-ItemProperty $key -ErrorAction Stop).'(default)' } catch {}
  if ($obsDir) { break }
}
if (-not $obsDir) { $obsDir = Join-Path $env:ProgramFiles 'obs-studio' }
$obsBin = Join-Path $obsDir 'bin\64bit'
if (-not (Test-Path (Join-Path $obsBin 'obs.dll'))) { throw "OBS Studio not found (looked in $obsDir)." }
$obsVersion = (Get-Item (Join-Path $obsBin 'obs64.exe')).VersionInfo.ProductVersion
Step "OBS $obsVersion at $obsDir"
if ($obsVersion -notlike '33.*') { Write-Warning "Headers/Qt are pinned to OBS $ObsTag; you have $obsVersion. Update the pins at the top of this script if the build fails or OBS rejects the plugin." }

# ---- Visual Studio environment --------------------------------------------------------------
$vcvars = $null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (Test-Path $vswhere) {
  $vs = & $vswhere -latest -prerelease -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
  if ($vs) { $vcvars = Join-Path $vs 'VC\Auxiliary\Build\vcvars64.bat' }
}
if (-not $vcvars -or -not (Test-Path $vcvars)) {
  $vcvars = Get-ChildItem "$env:ProgramFiles\Microsoft Visual Studio", "${env:ProgramFiles(x86)}\Microsoft Visual Studio" -Recurse -Depth 6 -Filter vcvars64.bat -ErrorAction SilentlyContinue |
    Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $vcvars) { throw 'Visual Studio C++ tools not found. Install "Build Tools for Visual Studio" with "Desktop development with C++".' }
Step "MSVC environment: $vcvars"
cmd /c "`"$vcvars`" >nul 2>nul && set" | ForEach-Object { if ($_ -match '^([^=]+)=(.*)$') { Set-Item "env:$($Matches[1])" $Matches[2] } }
foreach ($tool in 'cl', 'lib', 'dumpbin', 'cmake', 'ninja') {
  if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) { throw "$tool not found after vcvars64 (install the C++ CMake tools component)." }
}

# ---- dependencies (once) --------------------------------------------------------------------
$dl = Join-Path $Deps 'dl'
New-Item -ItemType Directory -Force $dl | Out-Null
function Fetch($url, $file, $sha) {
  $path = Join-Path $dl $file
  if (-not (Test-Path $path)) { Step "Downloading $file"; Invoke-WebRequest -UseBasicParsing $url -OutFile "$path.part"; Move-Item "$path.part" $path }
  if ($sha -and (Get-FileHash $path -Algorithm SHA256).Hash -ne $sha.ToUpper()) { Remove-Item $path; throw "$file failed its SHA-256 check (deleted, run again)." }
  return $path
}
$qtZip = Fetch "https://github.com/obsproject/obs-deps/releases/download/$DepsVersion/windows-deps-qt6-$DepsVersion-x64.zip" 'qt6.zip' $QtSha256
$srcTar = Fetch "https://codeload.github.com/obsproject/obs-studio/tar.gz/refs/tags/$ObsTag" 'obs-src.tar.gz' $null
$panelHpp = Fetch "https://raw.githubusercontent.com/obsproject/obs-browser/$BrowserCommit/panel/browser-panel.hpp" 'browser-panel.hpp' $null

$qtDir = Join-Path $Deps 'qt6'
if (-not (Test-Path (Join-Path $qtDir 'lib\cmake\Qt6'))) {
  Step 'Unpacking Qt SDK'
  New-Item -ItemType Directory -Force $qtDir | Out-Null
  tar -xf $qtZip -C $qtDir
  if ($LASTEXITCODE) { throw 'Unpacking Qt failed.' }
}

if ($Clean -and (Test-Path $Sdk)) { Remove-Item -Recurse -Force $Sdk }
if (-not (Test-Path (Join-Path $Sdk 'include\libobs\obs-module.h'))) {
  Step 'Preparing OBS headers'
  $src = Join-Path $Deps 'src'
  New-Item -ItemType Directory -Force $src, "$Sdk\include\browser" | Out-Null
  tar -xzf $srcTar -C $src "obs-studio-$ObsTag/libobs" "obs-studio-$ObsTag/frontend/api"
  if ($LASTEXITCODE) { throw 'Unpacking OBS sources failed.' }
  Copy-Item -Recurse -Force (Join-Path $src "obs-studio-$ObsTag\libobs") "$Sdk\include\libobs"
  Copy-Item -Recurse -Force (Join-Path $src "obs-studio-$ObsTag\frontend\api") "$Sdk\include\frontend-api"
  Copy-Item -Force $panelHpp "$Sdk\include\browser\browser-panel.hpp"
  # Normally generated by OBS's own CMake build (values of the Windows release).
  Set-Content -Encoding ascii "$Sdk\include\libobs\obsconfig.h" @(
    '#pragma once', '#define OBS_DATA_PATH "../../data"', '#define OBS_PLUGIN_PATH "../../obs-plugins/64bit"',
    '#define OBS_PLUGIN_DESTINATION "obs-plugins/64bit"', '#define OBS_RELEASE_CANDIDATE 0', '#define OBS_BETA 0',
    '#ifndef OBS_INSTALL_PREFIX', '#define OBS_INSTALL_PREFIX ""', '#endif')
}

# Import libraries generated from the installed DLLs: always rebuilt, so they track OBS updates.
Step 'Creating import libraries from your OBS'
New-Item -ItemType Directory -Force "$Sdk\lib" | Out-Null
foreach ($name in 'obs', 'obs-frontend-api') {
  $exports = dumpbin /nologo /exports (Join-Path $obsBin "$name.dll")
  $names = $exports | ForEach-Object { if ($_ -match '^\s+\d+\s+[0-9A-F]+\s+[0-9A-F]{8}\s+(\S+)') { $Matches[1] } }
  if (-not $names) { throw "No exports found in $name.dll" }
  Set-Content -Encoding ascii "$Sdk\lib\$name.def" (@("LIBRARY `"$name`"", 'EXPORTS') + ($names | ForEach-Object { "  $_" }))
  lib /nologo /machine:x64 "/def:$Sdk\lib\$name.def" "/out:$Sdk\lib\$name.lib" | Out-Null
  if ($LASTEXITCODE) { throw "lib.exe failed for $name" }
}

# ---- build ----------------------------------------------------------------------------------
if ($Clean -and (Test-Path $Build)) { Remove-Item -Recurse -Force $Build }
Step 'Configuring'
cmake -S $Root -B $Build -G Ninja -DCMAKE_BUILD_TYPE=Release "-DCMAKE_PREFIX_PATH=$qtDir" "-DPIXELPAW_OBS_SDK=$Sdk" | Out-Host
if ($LASTEXITCODE) { throw 'CMake configure failed.' }
Step 'Compiling'
cmake --build $Build | Out-Host
if ($LASTEXITCODE) { throw 'Build failed.' }

Step "Packaging -> $Dist"
if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
New-Item -ItemType Directory -Force "$Dist\bin\64bit" | Out-Null
Copy-Item (Join-Path $Build 'pixelpaw.dll') "$Dist\bin\64bit\"
Copy-Item -Recurse (Join-Path $Root 'data') "$Dist\data"
Write-Host "Built: $Dist\bin\64bit\pixelpaw.dll" -ForegroundColor Green

if ($Install) {
  $app = Split-Path (Split-Path $Root)  # the PixelPaw folder
  $py = Join-Path $app '.venv\Scripts\python.exe'
  if (-not (Test-Path $py)) { $py = 'python' }
  Push-Location $app
  try { & $py -m pixelpaw.obsplugin install; if ($LASTEXITCODE) { throw 'Install failed.' } } finally { Pop-Location }
}
