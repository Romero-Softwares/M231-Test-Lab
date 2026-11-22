@echo off
setlocal EnableExtensions

set "BASE_DIR=%~dp0"
set "INSTALLER=%BASE_DIR%usb_machine_emulator\apply_driver_update.ps1"
set "PACKAGE=%BASE_DIR%usb_machine_emulator\driver\x64\Release\M231VirtualUsb\M231VirtualUsb.inf"
set "CERTIFICATE=%BASE_DIR%usb_machine_emulator\driver\x64\Release\M231VirtualUsb.cer"
set "RESULT=%BASE_DIR%usb_machine_emulator\.runtime\driver-update.result"
set "LOG=%BASE_DIR%usb_machine_emulator\.runtime\driver-update.log"

where powershell.exe >nul 2>&1
if errorlevel 1 (
    echo ERRO: O Windows PowerShell nao foi encontrado.
    exit /b 2
)

if not exist "%INSTALLER%" (
    echo ERRO: Instalador nao encontrado:
    echo %INSTALLER%
    exit /b 3
)

if not exist "%PACKAGE%" (
    echo ERRO: Pacote do driver nao encontrado:
    echo %PACKAGE%
    exit /b 4
)

if not exist "%CERTIFICATE%" (
    echo ERRO: Certificado do driver nao encontrado:
    echo %CERTIFICATE%
    exit /b 5
)

if /I "%~1"=="--check" (
    echo Pacote do driver pronto para instalacao.
    echo INF: %PACKAGE%
    echo Certificado: %CERTIFICATE%
    exit /b 0
)

powershell.exe -NoProfile -Command "$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent()); if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 goto elevate
goto install

:elevate
if /I "%~1"=="--elevated" (
    echo ERRO: Nao foi possivel obter permissao de Administrador.
    exit /b 6
)

echo Solicitando permissao de Administrador para instalar o driver...
set "M231_DRIVER_BAT=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$process = Start-Process -FilePath $env:M231_DRIVER_BAT -ArgumentList '--elevated' -Verb RunAs -Wait -PassThru; exit $process.ExitCode"
exit /b %ERRORLEVEL%

:install
echo Instalando o driver M231 Virtual USB...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%INSTALLER%" -Configuration Release
set "INSTALL_EXIT=%ERRORLEVEL%"

if not "%INSTALL_EXIT%"=="0" (
    echo.
    echo ERRO: A instalacao falhou com codigo %INSTALL_EXIT%.
    if exist "%RESULT%" type "%RESULT%"
    echo Log: %LOG%
    exit /b %INSTALL_EXIT%
)

if not exist "%RESULT%" (
    echo ERRO: O instalador terminou sem gerar o arquivo de resultado.
    echo Log: %LOG%
    exit /b 7
)

set /p "INSTALL_RESULT="<"%RESULT%"
if /I not "%INSTALL_RESULT%"=="SUCCESS" (
    echo ERRO: %INSTALL_RESULT%
    echo Log: %LOG%
    exit /b 8
)

echo.
echo Driver M231 Virtual USB instalado e emulador iniciado com sucesso.
echo O Gerenciador de Dispositivos deve mostrar USB Serial Converter e uma porta COM FTDI.
echo Log: %LOG%
exit /b 0
