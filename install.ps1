# Installs or updates WinLauncher for the current user, without administrator rights:
#
#     irm https://raw.githubusercontent.com/bjspi/WinLauncher/main/install.ps1 | iex
#
# Takes the newest GitHub release through pipx. It needs neither git nor a Rust compiler.
# Python and pipx are installed first when they are missing.
#
# Keep this file ASCII: Windows PowerShell 5.1 decodes a downloaded script without
# a charset as Latin-1. Never use `exit` either: under `iex` it would close the
# user's terminal, so failures are reported and the terminal stays open.

& {
    $ErrorActionPreference = 'Stop'

    $repository = 'bjspi/WinLauncher'
    $appName = 'winlauncher'
    $oldestMinor = 11  # pyproject.toml: requires-python.
    $newestMinor = 14  # pyproject.toml: the `rust` extra stops before 3.15.
    $requiredPlatform = 'win-amd64'  # The only platform the Rust engine wheel is built for.
    $wingetPython = '3.13'

    function Write-Step([string]$Text) {
        Write-Host "==> $Text" -ForegroundColor Cyan
    }

    # Tells a usable interpreter from the Microsoft Store stub, old versions and venvs,
    # which pipx must not build on because they may be deleted at any time.
    function Get-UsablePython([string]$Command, [string[]]$Prefix) {
        if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { return $null }
        $probe = "import sys,sysconfig;print(sys.version_info[0],sys.version_info[1]," +
            "sysconfig.get_platform(),sys.prefix!=sys.base_prefix,sys.executable,sep='|')"
        # A native program's stderr must not count as a PowerShell error here.
        $ErrorActionPreference = 'Continue'
        $output = & $Command @Prefix -c $probe 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
        $major, $minor, $platform, $inVenv, $executable = "$output".Trim().Split('|')
        if ($major -ne '3' -or $platform -ne $requiredPlatform -or $inVenv -eq 'True') {
            return $null
        }
        if ([int]$minor -lt $oldestMinor -or [int]$minor -gt $newestMinor) { return $null }
        return [pscustomobject]@{ Minor = [int]$minor; Executable = $executable }
    }

    function Find-Python {
        $found = @(
            foreach ($minor in $newestMinor..$oldestMinor) {
                Get-UsablePython 'py' @("-3.$minor")
            }
            Get-UsablePython 'python' @()
            Get-UsablePython 'python3' @()
        ) | Where-Object { $_ }
        if (-not $found) { return $null }
        return ($found | Sort-Object Minor -Descending | Select-Object -First 1).Executable
    }

    # New entries of the user's PATH reach this session, and the programs it starts.
    function Update-SessionPath {
        $known = $env:Path -split ';'
        foreach ($entry in [Environment]::GetEnvironmentVariable('Path', 'User') -split ';') {
            if ($entry -and $known -notcontains $entry) { $env:Path += ";$entry" }
        }
    }

    function Install-Python {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            throw "Python 3.$oldestMinor to 3.$newestMinor (x64) is required. " +
                'Install it from https://www.python.org/downloads/windows/ and run this again.'
        }
        Write-Step "Installing Python $wingetPython with winget"
        & winget install --exact --id "Python.Python.$wingetPython" --scope user --silent `
            --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget could not install Python $wingetPython." }
        Update-SessionPath
        $python = Find-Python
        if (-not $python) { throw 'Python was installed but cannot be found. Open a new terminal.' }
        return $python
    }

    function Test-PipxCommand([string[]]$Command) {
        $ErrorActionPreference = 'Continue'
        $program, $arguments = $Command
        if (-not (Get-Command $program -ErrorAction SilentlyContinue)) { return $false }
        & $program @arguments --version *> $null
        return $LASTEXITCODE -eq 0
    }

    # Returns how to call pipx. It must end up on PATH because WinLauncher itself
    # runs `pipx inject` when a plugin needs Python packages.
    function Get-Pipx([string]$Python) {
        if (Test-PipxCommand @('pipx')) { return , @('pipx') }
        $viaPython = @($Python, '-m', 'pipx')
        if (Test-PipxCommand $viaPython) { return , $viaPython }
        Write-Step 'Installing pipx'
        & $Python -m pip install --user --upgrade --disable-pip-version-check pipx | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'pip could not install pipx.' }
        return , $viaPython
    }

    function Invoke-Pipx([string[]]$Pipx, [string[]]$Arguments) {
        $program, $prefix = $Pipx
        $ErrorActionPreference = 'Continue'
        & $program @prefix @Arguments
    }

    function Add-PipxToPath([string[]]$Pipx) {
        Invoke-Pipx $Pipx @('ensurepath') *> $null
        Update-SessionPath
    }

    function Get-LatestRelease {
        # Windows PowerShell 5.1 may still default to older TLS, which GitHub refuses.
        [Net.ServicePointManager]::SecurityProtocol =
            [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        $url = "https://api.github.com/repos/$repository/releases/latest"
        return (Invoke-RestMethod -Uri $url -Headers @{ 'User-Agent' = 'WinLauncher-installer' }).tag_name
    }

    # Windows locks the files of the running version, so pipx could not replace them.
    function Stop-RunningApp {
        $running = Get-Process -Name $appName -ErrorAction SilentlyContinue
        if (-not $running) { return }
        Write-Step 'Closing the running WinLauncher'
        $running | Stop-Process -Force
        $running | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    }

    function Install-App([string[]]$Pipx, [string]$Python, [string]$Tag) {
        $version = $Tag.TrimStart('v')
        $wheel = "https://github.com/$repository/releases/download/$Tag/$appName-$version-py3-none-any.whl"
        Write-Step "Installing WinLauncher $version"
        Invoke-Pipx $Pipx @('install', '--force', '--python', $Python, "$appName[rust] @ $wheel") | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'pipx could not install WinLauncher.' }
    }

    function Start-App([string[]]$Pipx) {
        $binFolder = "$(Invoke-Pipx $Pipx @('environment', '--value', 'PIPX_BIN_DIR'))".Trim()
        $program = Join-Path $binFolder "$appName.exe"
        if (-not (Test-Path -LiteralPath $program)) {
            Write-Host "Installed, but $program was not found. Start it with: $appName --show"
            return
        }
        Write-Step 'Starting WinLauncher'
        Start-Process -FilePath $program -ArgumentList '--show'
    }

    try {
        $tag = Get-LatestRelease
        $python = Find-Python
        if (-not $python) { $python = Install-Python }
        Write-Step "Using Python: $python"
        $pipx = Get-Pipx $python
        Stop-RunningApp
        Install-App $pipx $python $tag
        Add-PipxToPath $pipx
        Start-App $pipx
        Write-Host 'Done. Press Ctrl twice to search; run this command again to update.' -ForegroundColor Green
    } catch {
        Write-Host "WinLauncher was not installed: $($_.Exception.Message)" -ForegroundColor Red
    }
}
