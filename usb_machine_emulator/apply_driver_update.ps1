param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtime = Join-Path $PSScriptRoot ".runtime"
$log = Join-Path $runtime "driver-update.log"
$result = Join-Path $runtime "driver-update.result"
$package = Join-Path $PSScriptRoot "driver\x64\$Configuration\M231VirtualUsb"
$inf = Join-Path $package "M231VirtualUsb.inf"
$certificate = Join-Path (Split-Path -Parent $package) "M231VirtualUsb.cer"
$stdout = Join-Path $runtime "current.stdout.log"
$stderr = Join-Path $runtime "current.stderr.log"

New-Item -ItemType Directory -Force -Path $runtime | Out-Null
Remove-Item -LiteralPath $result -Force -ErrorAction SilentlyContinue
Start-Transcript -LiteralPath $log -Force | Out-Null

try {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "O atualizador precisa ser executado como Administrador."
    }
    if (-not (Test-Path -LiteralPath $inf)) {
        throw "Pacote corrigido nao localizado: $inf"
    }
    if (-not (Test-Path -LiteralPath $certificate)) {
        throw "Certificado do pacote nao localizado: $certificate"
    }

    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -in @("python.exe", "pythonw.exe") -and
            $_.CommandLine -match "usb_machine_emulator(\\|/)run.py|usb_machine_emulator.run"
        } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

    certutil.exe -addstore -f Root $certificate
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao confiar no certificado na raiz local: $LASTEXITCODE"
    }
    certutil.exe -addstore -f TrustedPublisher $certificate
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao confiar no publicador do driver: $LASTEXITCODE"
    }

    pnputil.exe /add-driver $inf /install
    # 3010 significa que o pacote foi instalado e o Windows recomenda
    # reinicializacao. O controlador ja pode estar em execucao nesta sessao;
    # nao interrompa a inicializacao do emulador por esse retorno.
    if ($LASTEXITCODE -notin @(0, 3010)) {
        throw "Falha ao instalar o pacote corrigido: $LASTEXITCODE"
    }
    if ($LASTEXITCODE -eq 3010) {
        throw (
            "O pacote foi instalado, mas o Windows exige reinicializacao " +
            "antes de carregar o novo controlador USB virtual."
        )
    }
    # /add-driver apenas armazena o pacote. O barramento virtual so existe
    # depois que a instancia raiz Root\M231VirtualUsb e criada pelo devcon.
    # ROOT\USB\0000 e um identificador de outro enumerador e nunca deve ser
    # reiniciado por este atualizador.
    $rootHardwareId = "Root\M231VirtualUsb"
    $rootPresent = (pnputil.exe /enum-devices /deviceids | Out-String) -match
        "(?im)^ID da Instancia:\s+ROOT\\M231VIRTUALUSB\\"
    if (-not $rootPresent) {
        $devcon = Get-ChildItem -LiteralPath "${env:ProgramFiles(x86)}\Windows Kits\10\Tools" `
            -Recurse -Filter devcon.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if (-not $devcon) {
            throw "devcon.exe nao foi localizado no WDK para criar o controlador USB virtual."
        }
        & $devcon.FullName install $inf $rootHardwareId
        if ($LASTEXITCODE -ne 0) {
            throw "devcon nao conseguiu criar o controlador USB virtual: $LASTEXITCODE"
        }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "Python nao foi encontrado no PATH para iniciar o emulador USB."
    }

    # A enumeracao so confirma que algum pacote M231 foi carregado. Versoes
    # antigas tambem criam o conversor, mas somente o M231Pro e suportado
    # pelo contrato de sincronizacao da emulacao.
    # Valide o contrato antes de o emulador ocupar o canal do controlador.
    $contractCheck = @"
from usb_machine_emulator.driver_transport import VirtualUsbDriver

driver = VirtualUsbDriver()
driver.open()
try:
    status = driver.status_info()
finally:
    driver.close()
print(status.version)
raise SystemExit(0 if status.version >= 3 else 9)
"@
    Push-Location $projectRoot
    try {
        $contractOutput = @(& $pythonCommand.Source -c $contractCheck 2>&1)
        $contractExit = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    $contractVersion = ($contractOutput | Select-Object -Last 1)
    if ($contractExit -ne 0) {
        $contractDetails = ($contractOutput | Out-String).Trim()
        throw (
            "O driver M231 carregado nao fornece o contrato v3. " +
            "Saida da verificacao: $contractDetails"
        )
    }

    Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
    $emulator = Start-Process `
        -FilePath $pythonCommand.Source `
        -ArgumentList @("-u", "-m", "usb_machine_emulator.run") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    $converterId = "USB\VID_0403&PID_6001\A501GR6Y"
    $serialId = "FTDIBUS\VID_0403+PID_6001+A501GR6Y\0000"
    $converterReady = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        if ($emulator.HasExited) {
            $details = Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue
            throw "O emulador USB encerrou antes de criar o conversor. $details"
        }

        $devices = (pnputil.exe /enum-devices /connected /deviceids | Out-String)
        if ($devices.Contains($converterId) -and $devices.Contains($serialId)) {
            $converterReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $converterReady) {
        Stop-Process -Id $emulator.Id -Force -ErrorAction SilentlyContinue
        throw "O controlador iniciou, mas o conversor FTDI e a porta serial nao foram enumerados."
    }

    "SUCCESS" | Set-Content -LiteralPath $result -Encoding ascii
} catch {
    ("FAILED: " + $_.Exception.Message) | Set-Content -LiteralPath $result -Encoding utf8
    throw
} finally {
    Stop-Transcript | Out-Null
}
