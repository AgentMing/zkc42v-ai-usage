param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
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

    Push-Location $ProjectRoot
    try {
        & $python @arguments *>> $logPath
        if ($LASTEXITCODE -ne 0) {
            throw "quota refresh exited with code $LASTEXITCODE"
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
