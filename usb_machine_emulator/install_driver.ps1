param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Execute este script em um PowerShell elevado (Administrador)."
}

$package = Join-Path $PSScriptRoot "driver\x64\$Configuration\M231VirtualUsb"
$inf = Join-Path $package "M231VirtualUsb.inf"
if (-not (Test-Path -LiteralPath $inf)) {
    throw "Pacote nao compilado: $inf"
}

$certificate = Join-Path (Split-Path -Parent $package) "M231VirtualUsb.cer"
if (-not (Test-Path -LiteralPath $certificate)) {
    throw "Certificado de teste nao localizado: $certificate"
}

certutil.exe -addstore -f Root $certificate
if ($LASTEXITCODE -ne 0) {
    throw "Nao foi possivel confiar no certificado de teste como raiz local."
}

certutil.exe -addstore -f TrustedPublisher $certificate
if ($LASTEXITCODE -ne 0) {
    throw "Nao foi possivel confiar no certificado de teste como publicador local."
}

pnputil.exe /add-driver $inf /install
if ($LASTEXITCODE -ne 0) {
    throw "pnputil nao conseguiu adicionar o pacote ao Driver Store."
}

$devcon = Get-ChildItem -LiteralPath "${env:ProgramFiles(x86)}\Windows Kits\10\Tools" -Recurse -Filter devcon.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $devcon) {
    throw "devcon.exe nao foi localizado no WDK. O pacote foi adicionado, mas o dispositivo raiz ainda nao foi criado."
}

& $devcon.FullName install $inf "Root\M231VirtualUsb"
if ($LASTEXITCODE -ne 0) {
    throw "devcon nao conseguiu criar o controlador USB virtual."
}

Write-Host "M231 Virtual USB instalado. Reinicie o Windows se o Gerenciador de Dispositivos solicitar."
