$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    python -m usb_machine_emulator.run
} finally {
    Pop-Location
}
