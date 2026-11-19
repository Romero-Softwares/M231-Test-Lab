param(
    [Parameter(Mandatory = $true)]
    [string]$OriginalRoot
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$originalPath = (Resolve-Path -LiteralPath $OriginalRoot).Path
[Environment]::CurrentDirectory = $originalPath

$resolver = [ResolveEventHandler] {
    param($sender, $eventArgs)
    $assemblyName = ($eventArgs.Name -split ',')[0] + '.dll'
    $candidate = Join-Path $originalPath $assemblyName
    if (Test-Path -LiteralPath $candidate) {
        return [Reflection.Assembly]::LoadFrom($candidate)
    }
    return $null
}
[AppDomain]::CurrentDomain.add_AssemblyResolve($resolver)

function Send-Response {
    param([hashtable]$Value)
    [Console]::Out.WriteLine(($Value | ConvertTo-Json -Compress -Depth 5))
    [Console]::Out.Flush()
}

function Find-Control {
    param($Root, [string]$Name)
    foreach ($control in $Root.Controls) {
        if ($control.Name -eq $Name) {
            return $control
        }
        $nested = Find-Control -Root $control -Name $Name
        if ($null -ne $nested) {
            return $nested
        }
    }
    return $null
}

function Read-ControlText {
    param($Form, [string]$Name)
    $control = Find-Control -Root $Form -Name $Name
    if ($null -eq $control) {
        return ""
    }
    return [string]$control.Text
}

$assembly = [Reflection.Assembly]::LoadFrom((Join-Path $originalPath 'TESTPro.exe'))
$formType = $assembly.GetType('TESTERPro.MainForm', $true)
$form = [Activator]::CreateInstance($formType)
$form.ShowInTaskbar = $false
$form.WindowState = [System.Windows.Forms.FormWindowState]::Minimized
$form.Opacity = 0
$initialized = $false

$buttonMap = @{
    jog_up = 'BtnUp'
    jog_down = 'BtnDown'
    fast_up = 'BtnFastUp'
    fast_down = 'BtnFastDown'
    run = 'BtnRun'
    pause = 'BtnPause'
    stop = 'BtnStop'
    zero_force = 'BtnForceZero'
    zero_position = 'BtnPosZero'
    zero_length = 'BtnLengthZero'
    zero_all = 'BtnClears'
    return_force = 'BtnForceReturn'
    return_position = 'BtnPosReturn'
}

try {
    while ($true) {
        $line = [Console]::In.ReadLine()
        if ($null -eq $line) {
            break
        }
        try {
            $request = $line | ConvertFrom-Json
            switch ([string]$request.action) {
                'ping' {
                    Send-Response @{
                        ok = $true
                        version = $assembly.GetName().Version.ToString()
                        initialized = $initialized
                        commands = @($buttonMap.Keys)
                    }
                }
                'connect' {
                    if (-not $initialized) {
                        $form.Show()
                        [System.Windows.Forms.Application]::DoEvents()
                        $form.Hide()
                        $initialized = $true
                    }
                    Send-Response @{ ok = $true; initialized = $initialized }
                }
                'command' {
                    $commandName = [string]$request.name
                    if (-not $buttonMap.ContainsKey($commandName)) {
                        throw "Comando legado desconhecido: $commandName"
                    }
                    if ($null -ne $request.speed_mm_min) {
                        $speedControl = Find-Control -Root $form -Name 'SpeedSet'
                        if ($null -ne $speedControl) {
                            $speedControl.Text = [string]$request.speed_mm_min
                        }
                    }
                    $button = Find-Control -Root $form -Name $buttonMap[$commandName]
                    if ($null -eq $button) {
                        throw "Controle $($buttonMap[$commandName]) nao encontrado."
                    }
                    $button.PerformClick()
                    [System.Windows.Forms.Application]::DoEvents()
                    Send-Response @{ ok = $true; command = $commandName }
                }
                'telemetry' {
                    [System.Windows.Forms.Application]::DoEvents()
                    Send-Response @{
                        ok = $true
                        force = Read-ControlText -Form $form -Name 'ForceDisp'
                        position = Read-ControlText -Form $form -Name 'PositionDisp'
                        length = Read-ControlText -Form $form -Name 'LengthDisp'
                        rate = Read-ControlText -Form $form -Name 'Rate'
                        state = Read-ControlText -Form $form -Name 'LblRunState'
                        movement = Read-ControlText -Form $form -Name 'LblMoveState'
                        limit = Read-ControlText -Form $form -Name 'LblLimitState'
                    }
                }
                'shutdown' {
                    if ($initialized) {
                        try {
                            $stopButton = Find-Control -Root $form -Name 'BtnStop'
                            if ($null -ne $stopButton) {
                                $stopButton.PerformClick()
                            }
                        } catch {
                        }
                    }
                    Send-Response @{ ok = $true }
                    break
                }
                default {
                    throw "Acao desconhecida: $($request.action)"
                }
            }
            if ([string]$request.action -eq 'shutdown') {
                break
            }
        } catch {
            Send-Response @{ ok = $false; error = $_.Exception.Message }
        }
    }
} finally {
    $form.Dispose()
    [AppDomain]::CurrentDomain.remove_AssemblyResolve($resolver)
}
