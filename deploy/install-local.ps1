param(
    [string]$ProxySubUrl = "",
    [switch]$NoOpen
)

$ErrorActionPreference = "Stop"
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Require-Command {
    param([string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "$Name is required. Please install Docker Desktop and make sure Docker Compose is available."
    }
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )
    $line = "$Key=$Value"
    if (-not (Test-Path $Path)) {
        Set-Content -Path $Path -Value $line -Encoding utf8
        return
    }
    $content = Get-Content -Path $Path -ErrorAction SilentlyContinue
    $found = $false
    $escapedKey = [regex]::Escape($Key)
    $next = foreach ($item in $content) {
        if ($item -match "^$escapedKey=") {
            $found = $true
            $line
        } else {
            $item
        }
    }
    if (-not $found) {
        $next = @($next) + $line
    }
    Set-Content -Path $Path -Value $next -Encoding utf8
}

function Get-EnvValue {
    param(
        [string]$Path,
        [string]$Key,
        [string]$DefaultValue
    )
    if (Test-Path $Path) {
        $escapedKey = [regex]::Escape($Key)
        $match = Get-Content -Path $Path | Where-Object { $_ -match "^$escapedKey=" } | Select-Object -First 1
        if ($match) {
            return ($match -replace "^$escapedKey=", "")
        }
    }
    return $DefaultValue
}


function Set-YamlScalar {
    param(
        [string]$Path,
        [string]$Key,
        [string]$Value
    )
    $content = if (Test-Path $Path) { @(Get-Content -Path $Path) } else { @() }
    $escapedKey = [regex]::Escape($Key)
    $found = $false
    $updated = foreach ($line in $content) {
        if ($line -match "^${escapedKey}:") {
            $found = $true
            "${Key}: ${Value}"
        } else {
            $line
        }
    }
    if (-not $found) {
        $updated = @($updated) + "${Key}: ${Value}"
    }
    Set-Content -Path $Path -Value $updated -Encoding utf8
}

function Get-ProxyControllerSecret {
    # Same order as refresh_proxy.sh: PROXY_CONTROLLER_SECRET, then the secret
    # kept under state/, otherwise a new random one saved there. Keeping it out
    # of config.yaml means a subscription refresh writes the same value back.
    $secretPath = "state/proxy/controller-secret"
    $secret = $env:PROXY_CONTROLLER_SECRET
    if (-not $secret) {
        $secret = (Get-EnvValue -Path ".env" -Key "PROXY_CONTROLLER_SECRET" -DefaultValue "").Trim().Trim('"', "'")
    }
    if ($secret) {
        Write-Host "Proxy controller secret: using PROXY_CONTROLLER_SECRET."
    } elseif ((Test-Path $secretPath) -and (Get-Item $secretPath).Length -gt 0) {
        $secret = (Get-Content -Path $secretPath -Raw).Trim()
        Write-Host "Proxy controller secret: keeping $secretPath."
    } else {
        New-Item -ItemType Directory -Force -Path (Split-Path $secretPath) | Out-Null
        $bytes = New-Object byte[] 16
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $secret = -join ($bytes | ForEach-Object { $_.ToString("x2") })
        # ASCII without a BOM so refresh_proxy.sh reads back the same value.
        [System.IO.File]::WriteAllText((Join-Path (Get-Location) $secretPath), "$secret`n", [System.Text.Encoding]::ASCII)
        Write-Host "Proxy controller secret: generated $secretPath."
    }
    if ($secret -notmatch '^[A-Za-z0-9._~-]+$') {
        throw "Proxy controller secret may only contain letters, digits and . _ ~ -"
    }
    return $secret
}

function Initialize-ProxyConfig {
    # Resolve the secret first so a rejected value never leaves a freshly
    # downloaded config without one.
    $controllerSecret = Get-ProxyControllerSecret
    $configPath = "proxy/config.yaml"
    $examplePath = "proxy/config.example.yaml"
    $subscription = Get-EnvValue -Path ".env" -Key "PROXY_SUB_URL" -DefaultValue ""
    $userAgent = Get-EnvValue -Path ".env" -Key "PROXY_USER_AGENT" -DefaultValue "clash-verge/1.7.7"

    if ($subscription) {
        $tempPath = "$configPath.tmp"
        try {
            Invoke-WebRequest -Uri $subscription -Headers @{ "User-Agent" = $userAgent } -OutFile $tempPath -UseBasicParsing
            Move-Item -Force $tempPath $configPath
            Write-Host "Proxy subscription refreshed: $configPath"
        } finally {
            Remove-Item -Force $tempPath -ErrorAction SilentlyContinue
        }
    } elseif (-not (Test-Path $configPath)) {
        Copy-Item $examplePath $configPath
        Write-Host "PROXY_SUB_URL is empty. Created a DIRECT-only proxy config."
    }

    Set-YamlScalar -Path $configPath -Key "mixed-port" -Value "7890"
    Set-YamlScalar -Path $configPath -Key "allow-lan" -Value "true"
    Set-YamlScalar -Path $configPath -Key "bind-address" -Value "'*'"
    Set-YamlScalar -Path $configPath -Key "external-controller" -Value "'0.0.0.0:9090'"
    Set-YamlScalar -Path $configPath -Key "secret" -Value "'$controllerSecret'"
}

Require-Command docker
docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose is unavailable. Please install or enable the Compose plugin."
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
}

if ($ProxySubUrl) {
    Set-EnvValue -Path ".env" -Key "PROXY_SUB_URL" -Value $ProxySubUrl
}

# Validate quietly: resolved configuration may contain secrets.
docker compose config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose configuration is invalid. Check .env and docker-compose.yml."
}
docker info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker is not reachable. Start Docker Desktop and check your Docker context."
}

New-Item -ItemType Directory -Force -Path "proxy", "state/cron", "state/login-api", "state/login-profile", "state/browser-profiles", "DouYinSparkFlow/logs" | Out-Null
if (-not (Test-Path "proxy/config.yaml")) {
    Copy-Item "proxy/config.example.yaml" "proxy/config.yaml"
}
if (-not (Test-Path "state/cron/root") -or (Get-Item "state/cron/root").Length -eq 0) {
    @(
        "*/20 10-17 * * * cd /app && python main.py --doTask >> /app/logs/app.log 2>&1",
        "0 18 * * * cd /app && python main.py --doTask >> /app/logs/app.log 2>&1",
        "20 18 * * * cd /app && env SPARKFLOW_MANUAL_RUN=1 SPARKFLOW_MANUAL_UNSENT_ONLY=1 PYTHONUNBUFFERED=1 python main.py --doTask >> /app/logs/app.log 2>&1"
    ) | Set-Content -Path "state/cron/root" -Encoding utf8
}

Initialize-ProxyConfig

docker compose up -d --build
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to build or start the services. Review the Docker output above."
}

$webPort = Get-EnvValue -Path ".env" -Key "WEB_PORT" -DefaultValue "8787"
$url = "http://localhost:$webPort"
Write-Host "Douyin SparkFlow is running: $url"
Write-Host "Next: create the admin password, open the login desktop, scan the QR code, select target friends, and set the send window."
if (-not $NoOpen) {
    Start-Process $url
}
