param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$Partial,
    [switch]$PartialRed
)

$ErrorActionPreference = 'Stop'
# Set OLLAMA_API_KEY and, if needed, WINDSURF_API_KEY in the user/system environment before installing the
# scheduled task; never put API keys in data\quota-service.json or this file.
$configPath = Join-Path $ProjectRoot 'data\quota-service.json'
$outDir = Join-Path $ProjectRoot 'data\out'
$logPath = Join-Path $outDir 'quota-service.log'

New-Item -ItemType Directory -Path $outDir -Force | Out-Null

try {
    if (-not (Test-Path -LiteralPath $configPath)) {
        throw "Missing local service config: $configPath"
    }

    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if (-not $config.deviceAddress) {
        throw 'deviceAddress is missing from the local service config'
    }

    $env:EPAPER_UUID = [string]$config.deviceAddress
    $env:PYTHONPATH = Join-Path $ProjectRoot 'tools'

    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $arguments = @(
        '-m', 'quotas',
        '--out-dir', $outDir,
        '--send',
        '--bleprobe', (Join-Path $ProjectRoot 'bleprobe.py')
    )
    if ($Partial) {
        $arguments += '--partial'
        if ($PartialRed) {
            $arguments += '--partial-red'
        }
    }

    Push-Location $ProjectRoot
    try {
        # Windows PowerShell 5.1 promotes native stderr to an error record.
        # The quota CLI writes normal progress (including BLE commands) there,
        # so keep it in the log without turning a successful refresh into a
        # failed scheduled-task result.
        $savedErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $python @arguments *>> $logPath
            $pythonExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $savedErrorActionPreference
        }
        if ($pythonExitCode -ne 0) {
            throw "quota refresh exited with code $pythonExitCode"
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] ERROR: $($_.Exception.Message)" |
        Out-File -LiteralPath $logPath -Append -Encoding utf8
    exit 1
}
