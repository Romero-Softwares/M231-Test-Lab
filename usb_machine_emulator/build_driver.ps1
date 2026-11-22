param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$driverProject = Join-Path $PSScriptRoot "driver\M231VirtualUsb.vcxproj"
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"

if (-not (Test-Path -LiteralPath $vswhere)) {
    throw "Visual Studio Installer/vswhere nao foi localizado."
}

$msbuild = & $vswhere -latest -products "*" -requires Microsoft.Component.MSBuild -find "MSBuild\**\Bin\amd64\MSBuild.exe" | Select-Object -First 1
if (-not $msbuild) {
    throw "MSBuild do Visual Studio nao foi localizado."
}

& $msbuild $driverProject /restore /m /p:Configuration=$Configuration /p:Platform=x64
if ($LASTEXITCODE -ne 0) {
    throw "A compilacao do driver falhou com codigo $LASTEXITCODE."
}

$output = Join-Path $PSScriptRoot "driver\x64\$Configuration"
Write-Host "Driver compilado em $output"
