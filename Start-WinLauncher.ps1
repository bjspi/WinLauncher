param(
    [switch]$Show,
    [switch]$Background,
    [switch]$NoHooks,
    [string]$DataDir
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$localPython = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$pythonExecutable = if (Test-Path -LiteralPath $localPython) {
    $localPython
} else {
    (Get-Command pythonw.exe -ErrorAction Stop).Source
}
$arguments = @('-m', 'winlauncher')
if ($Show) { $arguments += '--show' }
if ($Background) { $arguments += '--background' }
if ($NoHooks) { $arguments += '--no-hooks' }
if ($DataDir) { $arguments += @('--data-dir', ('"' + $DataDir + '"')) }
Start-Process -FilePath $pythonExecutable -WorkingDirectory $projectRoot -ArgumentList $arguments -WindowStyle Hidden
