<#
.SYNOPSIS
    Start Docker (if needed), bring up %USERPROFILE%\ai-stack (Langfuse :3000, Qdrant :6333),
    and ensure .env exists next to vibe-ops-center (copy from .env.example only if missing).

.NOTES
    Windows Credential Manager cannot be read non-interactively without extra modules
    and known credential targets. Edit .env after first run with real API keys.
#>
$ErrorActionPreference = "Stop"

function Test-DockerDaemon {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    docker info *> $null
    $ok = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prev
    return $ok
}

function Start-DockerDesktopIfNeeded {
    if (Test-DockerDaemon) { return }
    $dd = Join-Path ${env:ProgramFiles} "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path $dd)) {
        Write-Error "Docker Desktop not found at $dd. Install Docker Desktop or start the daemon manually."
    }
    Write-Host "Starting Docker Desktop..."
    Start-Process -FilePath $dd -WindowStyle Hidden
    $maxWaitSec = 120
    $step = 2
    for ($t = 0; $t -lt $maxWaitSec; $t += $step) {
        Start-Sleep -Seconds $step
        if (Test-DockerDaemon) {
            Write-Host "Docker daemon is up (${t}s)."
            return
        }
        Write-Host "  waiting for Docker... (${t}s / ${maxWaitSec}s)"
    }
    Write-Error "Docker daemon did not become ready within ${maxWaitSec}s."
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $env:USERPROFILE "ai-stack\docker-compose.yml"

if (-not (Test-Path $compose)) {
    Write-Error "Compose file not found: $compose`nExpected the same layout as in actions.py (~/ai-stack/docker-compose.yml)."
}

Start-DockerDesktopIfNeeded

Write-Host "docker compose -f $compose up -d"
docker compose -f $compose up -d

Write-Host "`nService status:"
docker compose -f $compose ps

$envFile = Join-Path $repoRoot ".env"
$example = Join-Path $repoRoot ".env.example"
if (-not (Test-Path $envFile)) {
    if (Test-Path $example) {
        Copy-Item -Path $example -Destination $envFile
        Write-Host "`nCreated $envFile from .env.example - open it and paste your N8N / Langfuse keys."
    } else {
        Write-Warning ".env missing and no .env.example found at $example"
    }
} else {
    Write-Host "`n.env already exists - not overwriting."
}

Write-Host "`nNext: edit $envFile if keys are still placeholders, then run: python main.py"
