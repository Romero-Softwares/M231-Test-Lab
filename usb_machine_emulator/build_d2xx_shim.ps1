param(
    [ValidateSet("Release", "Debug")]
    [string]$Configuration = "Release"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "d2xx_shim\virtual_d2xx.c"
$x86Def = Join-Path $PSScriptRoot "d2xx_shim\virtual_d2xx_x86.def"
$x64Def = Join-Path $PSScriptRoot "d2xx_shim\virtual_d2xx_x64.def"
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
$vcvars = & $vswhere -latest -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -find "VC\Auxiliary\Build\vcvarsall.bat" | Select-Object -First 1
if (-not $vcvars) {
    throw "Visual C++ x86/x64 nao foi localizado."
}

$output = Join-Path $PSScriptRoot "d2xx_shim\bin"
New-Item -ItemType Directory -Force -Path $output | Out-Null
$optimization = if ($Configuration -eq "Release") { "/O2" } else { "/Od /Zi" }

function Build-Shim([string]$Architecture, [string]$Definition, [string]$Destination) {
    $target = Join-Path $output ("FTD2XX-" + $Architecture + ".DLL")
    $object = Join-Path $output ("virtual_d2xx-" + $Architecture + ".obj")
    $importLibrary = Join-Path $output ("virtual_d2xx-" + $Architecture + ".lib")
    $command = 'call "{0}" {1} >nul && cl.exe /nologo /LD /W4 /WX /MT {2} /Fo"{3}" "{4}" /link /DEF:"{5}" /OUT:"{6}" /IMPLIB:"{7}"' -f $vcvars, $Architecture, $optimization, $object, $source, $Definition, $target, $importLibrary
    & cmd.exe /d /c $command
    if ($LASTEXITCODE -ne 0) {
        throw "Falha ao compilar a D2XX virtual $Architecture."
    }
    Copy-Item -LiteralPath $target -Destination $Destination -Force
}

# O shim e somente uma ferramenta de diagnostico. Ele nunca pode substituir a
# D2XX oficial consumida pelo M231Pro. A emulação usa somente o protocolo
# nativo da controladora e não deve receber a DLL virtual.
Build-Shim "x64" $x64Def (Join-Path $projectRoot "driver\virtual_d2xx64.dll")
Build-Shim "x86" $x86Def (Join-Path $projectRoot "driver\virtual_d2xx.dll")
Write-Host "Shims D2XX de diagnostico gerados sem substituir as DLLs FTDI oficiais."
