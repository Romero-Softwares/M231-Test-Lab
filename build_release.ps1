$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "C:\Users\Merotec\Desktop\Merotec_IDE\venv\Scripts\python.exe"
$Spec = Join-Path $ProjectRoot "M231Pro-Primeira-Versao.spec"
$ReleaseRoot = Join-Path $ProjectRoot "release"
$PackageName = "M231Pro-Primeira-Versao"
$PackageRoot = Join-Path $ReleaseRoot $PackageName
$ZipPath = Join-Path $ReleaseRoot "$PackageName-Windows-x64.zip"
$DriverPath = Join-Path $ProjectRoot "driver\ftd2xx64.dll"
$IconPath = Join-Path $ProjectRoot "assets\m231pro-icon.ico"

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Interpretador da IDE nao encontrado: $Python"
}
if (-not (Test-Path -LiteralPath $DriverPath -PathType Leaf)) {
    throw "DLL D2XX do projeto nao encontrada: $DriverPath"
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

$Forbidden = Get-ChildItem -LiteralPath $PackageRoot -Recurse -Force |
    Where-Object {
        $_.FullName -match "(?i)anytest|testpro|dao350|testdb|testing\.mdb"
    }
if ($Forbidden) {
    $Paths = ($Forbidden.FullName -join [Environment]::NewLine)
    throw "Arquivos legados detectados no pacote:`n$Paths"
}

$PackagedDrivers = @(Get-ChildItem -LiteralPath $PackageRoot -Recurse -File -Filter "ftd2xx*.dll")
if ($PackagedDrivers.Count -ne 1 -or $PackagedDrivers[0].Name -ne "ftd2xx64.dll") {
    throw "O pacote deve conter exatamente uma DLL D2XX x64."
}

if (Test-Path -LiteralPath $ZipPath -PathType Leaf) {
    Remove-Item -LiteralPath $ZipPath -Force
}
Compress-Archive -LiteralPath $PackageRoot -DestinationPath $ZipPath -CompressionLevel Optimal

$Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
Write-Output "PACKAGE=$PackageRoot"
Write-Output "ZIP=$ZipPath"
Write-Output "SHA256=$Hash"
Write-Output "DRIVER=$($PackagedDrivers[0].FullName)"
