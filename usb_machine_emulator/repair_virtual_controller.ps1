param(
    [ValidateSet("Release", "Debug")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$runtime = Join-Path $PSScriptRoot ".runtime"
$log = Join-Path $runtime "repair_elevated.log"
New-Item -ItemType Directory -Path $runtime -Force | Out-Null
Start-Transcript -LiteralPath $log -Force | Out-Null

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Execute este reparo como Administrador."
}

$package = Join-Path $PSScriptRoot "driver\\x64\\$Configuration\\M231VirtualUsb"
$inf = Join-Path $package "M231VirtualUsb.inf"
if (-not (Test-Path -LiteralPath $inf)) {
    throw "Pacote do driver nao localizado: $inf"
}

$devcon = Get-ChildItem -LiteralPath "${env:ProgramFiles(x86)}\\Windows Kits\\10\\Tools" `
    -Recurse -Filter devcon.exe -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like "*\x64\devcon.exe" } |
    Sort-Object FullName -Descending |
    Select-Object -First 1
if (-not $devcon) {
    throw "devcon.exe nao foi localizado no Windows Driver Kit."
}

try {
    # Primeiro armazena e associa o pacote novo. Em algumas versoes do
    # pnputil, /add-driver retorna 259 mesmo depois de informar sucesso; o
    # estado do Driver Store e a fonte confiavel, nao esse codigo isolado.
    pnputil.exe /add-driver $inf /install
    $installedPackage = Get-WindowsDriver -Online -All |
        Where-Object { $_.OriginalFileName -like "*m231virtualusb.inf" } |
        Sort-Object Date, Version -Descending |
        Select-Object -First 1
    if (-not $installedPackage) {
        throw "O pacote M231 nao permaneceu no Driver Store."
    }

    # A instancia quebrada e somente o host raiz do emulador. O FTDI fisico tem
    # outro ID (USB\\VID_0403&PID_6001) e nunca e removido por este reparo.
    foreach ($badInstance in @("ROOT\USB\0001", "ROOT\USB\0002")) {
        $badDevice = Get-PnpDevice -InstanceId $badInstance -ErrorAction SilentlyContinue
        $badHardwareIds = if ($badDevice) {
            (Get-PnpDeviceProperty -InstanceId $badInstance `
                -KeyName "DEVPKEY_Device_HardwareIds" `
                -ErrorAction SilentlyContinue).Data
        }
        if ($badHardwareIds -contains 'Root\\M231VirtualUsb') {
            pnputil.exe /remove-device $badInstance
        }
    }

    $instanceId = "ROOT\USB\0000"
    $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
    if ($device) {
        pnputil.exe /restart-device $instanceId
        $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
    }

    if ($device -and $device.Problem -notin @(0, 14)) {
        pnputil.exe /remove-device $instanceId
        $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
        if ($device) {
            throw "Nao foi possivel remover o host virtual com erro: $LASTEXITCODE"
        }
    }

    if (-not $device) {
        & $devcon.FullName install $inf "Root\M231VirtualUsb"
        $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
        if (-not $device) {
            throw "Nao foi possivel recriar o host virtual M231: $LASTEXITCODE"
        }
        pnputil.exe /restart-device $instanceId
        $device = Get-PnpDevice -InstanceId $instanceId -ErrorAction SilentlyContinue
    }

    if ($device.Problem -eq 14) {
        throw "REBOOT_REQUIRED: pacote novo instalado; reinicie o Windows para carregar o host virtual."
    }
    if ($device.Problem -ne 0) {
        throw "O host virtual continua com codigo $($device.Problem); reinicie o Windows para concluir a troca do driver."
    }

    Write-Host "Host virtual M231 reparado e em execucao."
    pnputil.exe /enum-devices /instanceid $instanceId /drivers
} finally {
    Stop-Transcript | Out-Null
}
