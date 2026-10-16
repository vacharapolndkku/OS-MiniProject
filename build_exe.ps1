[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$projectRoot = $PSScriptRoot
$buildEnvironment = Join-Path $projectRoot ".venv-build"
$buildPython = Join-Path $buildEnvironment "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $buildPython)) {
    & $Python -m venv $buildEnvironment
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
}

& $buildPython -m pip install --disable-pip-version-check --no-cache-dir -r (Join-Path $projectRoot "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install the build tools." }

$previousCache = [Environment]::GetEnvironmentVariable("PYINSTALLER_CONFIG_DIR", "Process")
try {
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot "build\pyinstaller-cache"
    & $buildPython -m PyInstaller --noconfirm --distpath (Join-Path $projectRoot "dist") --workpath (Join-Path $projectRoot "build") (Join-Path $projectRoot "SmallMission.spec")
    if ($LASTEXITCODE -ne 0) { throw "Executable build failed." }
}
finally {
    if ($null -eq $previousCache) {
        Remove-Item Env:PYINSTALLER_CONFIG_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:PYINSTALLER_CONFIG_DIR = $previousCache
    }
}

$executable = Join-Path $projectRoot "dist\Small Mission.exe"
if (-not (Test-Path -LiteralPath $executable)) { throw "Build finished without producing Small Mission.exe." }
Write-Host "Ready: $executable"
