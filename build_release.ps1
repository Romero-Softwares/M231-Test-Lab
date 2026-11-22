param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($Python)) {
    $Python = "C:\Users\Merotec\Desktop\Merotec_IDE\venv\Scripts\python.exe"
}
$Spec = Join-Path $ProjectRoot "M231Pro-Primeira-Versao.spec"
$ReleaseRoot = Join-Path $ProjectRoot "release"
$PackageName = "M231Pro-Primeira-Versao"
$BuiltPackageRoot = Join-Path $ReleaseRoot $PackageName
$DriverPath = Join-Path $ProjectRoot "driver\ftd2xx.dll"
$Driver64Path = Join-Path $ProjectRoot "driver\ftd2xx64.dll"
$VirtualDriverPath = Join-Path $ProjectRoot "driver\virtual_d2xx.dll"
$VirtualDriver64Path = Join-Path $ProjectRoot "driver\virtual_d2xx64.dll"
$IconPath = Join-Path $ProjectRoot "assets\m231pro-icon.ico"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Interpretador da IDE nao encontrado: $Python"
}
$PythonBits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $PythonBits -notin @('32', '64')) {
    throw "Nao foi possivel determinar a arquitetura do Python usado no build."
}
$ArchitectureLabel = "x$PythonBits"
$PackageRoot = Join-Path $ReleaseRoot "$PackageName-Windows-$ArchitectureLabel"
$ZipPath = Join-Path $ReleaseRoot "$PackageName-Windows-$ArchitectureLabel.zip"
if (-not (Test-Path -LiteralPath $DriverPath -PathType Leaf)) {
    throw "DLL D2XX do projeto nao encontrada: $DriverPath"
}
if (-not (Test-Path -LiteralPath $Driver64Path -PathType Leaf) -or
    -not (Test-Path -LiteralPath $VirtualDriverPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $VirtualDriver64Path -PathType Leaf)) {
    throw "O pacote precisa conter as DLLs D2XX x86 e x64, fisica e virtual."
}
if (-not (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    throw "Icone do aplicativo nao encontrado: $IconPath"
}

New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null

& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $ReleaseRoot `
    --workpath (Join-Path $ProjectRoot "build\pyinstaller") `
    $Spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller terminou com codigo $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath $BuiltPackageRoot -PathType Container)) {
    throw "O PyInstaller nao produziu o pacote esperado: $BuiltPackageRoot"
}
if (Test-Path -LiteralPath $PackageRoot) {
    Remove-Item -LiteralPath $PackageRoot -Recurse -Force
}
Move-Item -LiteralPath $BuiltPackageRoot -Destination $PackageRoot

$PackagedDrivers = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File |
    Where-Object { $_.Name -in @("ftd2xx.dll", "ftd2xx64.dll", "virtual_d2xx.dll", "virtual_d2xx64.dll") })
$PackagedDriverNames = @($PackagedDrivers | Select-Object -ExpandProperty Name | Sort-Object)
if ($PackagedDrivers.Count -ne 4 -or
    ($PackagedDriverNames -join ",") -ne "ftd2xx.dll,ftd2xx64.dll,virtual_d2xx.dll,virtual_d2xx64.dll") {
    throw "O pacote deve conter as DLLs D2XX física e virtual x86/x64."
}

if (Test-Path -LiteralPath $ZipPath -PathType Leaf) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal

$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
Write-Output "PACKAGE=$PackageRoot"
Write-Output "ZIP=$ZipPath"
Write-Output "SHA256=$Hash"
Write-Output "DRIVERS=$($PackagedDrivers.FullName -join ';')"
