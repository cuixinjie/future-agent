$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$configPath = Join-Path $repoRoot 'data/searxng/config/settings.yml'
if (-not (Test-Path -LiteralPath $configPath)) {
    New-Item -ItemType Directory -Force -Path (Split-Path $configPath) | Out-Null
    $randomBytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($randomBytes) } finally { $rng.Dispose() }
    $localSecret = [BitConverter]::ToString($randomBytes).Replace('-', '').ToLowerInvariant()
    $template = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'settings.yml.example'))
    [IO.File]::WriteAllText($configPath, $template.Replace('REPLACE_WITH_RANDOM_LOCAL_SECRET', $localSecret), [Text.UTF8Encoding]::new($false))
}
docker compose -f (Join-Path $PSScriptRoot 'compose.yaml') up -d
if ($LASTEXITCODE -ne 0) { throw 'SearXNG Docker startup failed.' }
Write-Host 'SearXNG: http://127.0.0.1:8888'
