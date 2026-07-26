param(
    [string]$EnvFile = "",
    [string]$PublicHost = "",
    [int]$BridgePort = 12800,
    [int]$McpPort = 12801,
    [int]$NanobotApiPort = 8900,
    [switch]$RestartBridge,
    [switch]$ConfigOnly
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunDir = Join-Path $RootDir ".run"
$Python = Join-Path $RootDir ".venv-nanobot\Scripts\python.exe"
$NanobotConfig = if ($env:NANOBOT_CONFIG) { $env:NANOBOT_CONFIG } else { Join-Path $RootDir "nanobot_config\config.json" }

function Import-EnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return
    }
    Get-Content $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $parts = $line.Split("=", 2)
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
}

function Get-LanIPv4 {
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric |
        Select-Object -First 1
    if ($route) {
        $addr = Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
            Select-Object -First 1
        if ($addr) {
            return $addr.IPAddress
        }
    }
    $fallback = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -match "^(10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|192\.168\.)" } |
        Select-Object -First 1
    if ($fallback) {
        return $fallback.IPAddress
    }
    return ""
}

function Test-Http {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return [int]$response.StatusCode
    } catch {
        if ($_.Exception.Response -and $_.Exception.Response.StatusCode) {
            return [int]$_.Exception.Response.StatusCode
        }
        return 0
    }
}

function Stop-PortProcess {
    param([int]$Port)
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        if ($connection.OwningProcess -and $connection.OwningProcess -ne $PID) {
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

function Stop-PidFile {
    param([string]$Name)
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        return
    }
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid) {
        Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

if (-not $EnvFile) {
    $EnvFile = if ($env:STACKCHAN_ENV_FILE) { $env:STACKCHAN_ENV_FILE } else { Join-Path $RunDir "stackchan-nanobot.env" }
}

New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $RootDir "nanobot_config\workspace") | Out-Null
Import-EnvFile $EnvFile

if (-not (Test-Path $Python)) {
    throw "Missing Python virtualenv: $Python"
}
if (-not (Test-Path $NanobotConfig)) {
    throw "Missing Nanobot config: $NanobotConfig"
}

$env:NANOBOT_WORKSPACE = if ($env:NANOBOT_WORKSPACE) { $env:NANOBOT_WORKSPACE } else { Join-Path $RootDir "nanobot_config\workspace" }
$env:STACKCHAN_BRIDGE_PORT = "$BridgePort"
$env:STACKCHAN_MCP_PORT = "$McpPort"
$env:NANOBOT_API_PORT = "$NanobotApiPort"
$env:STACKCHAN_INFERENCE_MODE = if ($env:STACKCHAN_INFERENCE_MODE) { $env:STACKCHAN_INFERENCE_MODE } else { "cloud" }
$env:STACKCHAN_PUBLIC_HOST = if ($PublicHost) { $PublicHost } elseif ($env:STACKCHAN_PUBLIC_HOST) { $env:STACKCHAN_PUBLIC_HOST } else { Get-LanIPv4 }

if (-not $env:STACKCHAN_PUBLIC_HOST) {
    throw "Cannot detect LAN IP. Pass -PublicHost or set STACKCHAN_PUBLIC_HOST."
}

$inferenceMode = $env:STACKCHAN_INFERENCE_MODE
$chatProvider = if ($env:STACKCHAN_CHAT_PROVIDER) { $env:STACKCHAN_CHAT_PROVIDER } else { "glm" }

if ($inferenceMode -eq "local") {
    $env:OPENAI_API_KEY = if ($env:STACKCHAN_LOCAL_CHAT_API_KEY) { $env:STACKCHAN_LOCAL_CHAT_API_KEY } else { "local-no-secret" }
    $env:OPENAI_BASE_URL = if ($env:STACKCHAN_LOCAL_CHAT_BASE_URL) { $env:STACKCHAN_LOCAL_CHAT_BASE_URL } else { "http://127.0.0.1:18080/v1" }
    $env:STACKCHAN_CHAT_MODEL = if ($env:STACKCHAN_LOCAL_CHAT_MODEL) { $env:STACKCHAN_LOCAL_CHAT_MODEL } else { "Qwen3-4B" }
    $env:STACKCHAN_CHAT_THINKING = "disabled"
    $env:STACKCHAN_COMPACT_PROMPT = if ($env:STACKCHAN_LOCAL_COMPACT_PROMPT) { $env:STACKCHAN_LOCAL_COMPACT_PROMPT } else { "1" }
    $env:STACKCHAN_SAFE_NANOBOT_TOOLS = if ($env:STACKCHAN_LOCAL_SAFE_NANOBOT_TOOLS) { $env:STACKCHAN_LOCAL_SAFE_NANOBOT_TOOLS } else { "none" }
    $env:STACKCHAN_ASR_PROVIDER = "local"
    $env:STACKCHAN_ASR_BASE_URL = if ($env:STACKCHAN_LOCAL_ASR_BASE_URL) { $env:STACKCHAN_LOCAL_ASR_BASE_URL } else { "http://127.0.0.1:18081/v1" }
    $env:STACKCHAN_ASR_API_KEY = if ($env:STACKCHAN_LOCAL_ASR_API_KEY) { $env:STACKCHAN_LOCAL_ASR_API_KEY } else { "local-no-secret" }
    $env:STACKCHAN_ASR_MODEL = if ($env:STACKCHAN_LOCAL_ASR_MODEL) { $env:STACKCHAN_LOCAL_ASR_MODEL } else { "SenseVoiceSmall" }
    $env:STACKCHAN_ASR_COMMAND = if ($env:STACKCHAN_LOCAL_ASR_COMMAND) { $env:STACKCHAN_LOCAL_ASR_COMMAND } else { "$Python $RootDir\scripts\stackchan_asr_openai.py" }
    $env:STACKCHAN_TTS_PROVIDER = "local"
    $env:STACKCHAN_TTS_BASE_URL = if ($env:STACKCHAN_LOCAL_TTS_BASE_URL) { $env:STACKCHAN_LOCAL_TTS_BASE_URL } else { "http://127.0.0.1:18081/v1" }
    $env:STACKCHAN_TTS_API_KEY = if ($env:STACKCHAN_LOCAL_TTS_API_KEY) { $env:STACKCHAN_LOCAL_TTS_API_KEY } else { "local-no-secret" }
    $env:STACKCHAN_TTS_MODEL = if ($env:STACKCHAN_LOCAL_TTS_MODEL) { $env:STACKCHAN_LOCAL_TTS_MODEL } else { "vits-melo-tts-zh_en" }
    $env:STACKCHAN_TTS_VOICE = if ($env:STACKCHAN_LOCAL_TTS_VOICE) { $env:STACKCHAN_LOCAL_TTS_VOICE } else { "default" }
    $env:STACKCHAN_TTS_RESPONSE_FORMAT = if ($env:STACKCHAN_LOCAL_TTS_RESPONSE_FORMAT) { $env:STACKCHAN_LOCAL_TTS_RESPONSE_FORMAT } else { "wav" }
    $env:STACKCHAN_TTS_COMMAND = if ($env:STACKCHAN_LOCAL_TTS_COMMAND) { $env:STACKCHAN_LOCAL_TTS_COMMAND } else { "$Python $RootDir\scripts\stackchan_tts_openai.py" }
    $env:NANOBOT_OPENAI_COMPAT_TIMEOUT_S = if ($env:STACKCHAN_LOCAL_CHAT_TIMEOUT_S) { $env:STACKCHAN_LOCAL_CHAT_TIMEOUT_S } else { "60" }
} else {
    if ($chatProvider -eq "deepseek") {
        $env:OPENAI_API_KEY = if ($env:STACKCHAN_CHAT_API_KEY) { $env:STACKCHAN_CHAT_API_KEY } else { $env:DEEPSEEK_API_KEY }
        $env:OPENAI_BASE_URL = if ($env:DEEPSEEK_BASE_URL) { $env:DEEPSEEK_BASE_URL } else { "https://api.deepseek.com" }
        $env:STACKCHAN_CHAT_MODEL = if ($env:STACKCHAN_CHAT_MODEL) { $env:STACKCHAN_CHAT_MODEL } else { "deepseek-v4-flash" }
    } else {
        $env:OPENAI_API_KEY = if ($env:STACKCHAN_CHAT_API_KEY) { $env:STACKCHAN_CHAT_API_KEY } elseif ($env:ZHIPU_API_KEY) { $env:ZHIPU_API_KEY } else { $env:GLM_API_KEY }
        $env:OPENAI_BASE_URL = if ($env:STACKCHAN_GLM_BASE_URL) { $env:STACKCHAN_GLM_BASE_URL } else { "https://open.bigmodel.cn/api/paas/v4" }
        $env:STACKCHAN_CHAT_MODEL = if ($env:STACKCHAN_CHAT_MODEL) { $env:STACKCHAN_CHAT_MODEL } else { "glm-4.7-flash" }
        $env:STACKCHAN_ASR_PROVIDER = if ($env:STACKCHAN_ASR_PROVIDER) { $env:STACKCHAN_ASR_PROVIDER } else { "glm" }
        $env:STACKCHAN_ASR_MODEL = if ($env:STACKCHAN_ASR_MODEL) { $env:STACKCHAN_ASR_MODEL } else { "glm-asr-2512" }
        $env:STACKCHAN_TTS_PROVIDER = if ($env:STACKCHAN_TTS_PROVIDER) { $env:STACKCHAN_TTS_PROVIDER } else { "glm" }
        $env:STACKCHAN_TTS_MODEL = if ($env:STACKCHAN_TTS_MODEL) { $env:STACKCHAN_TTS_MODEL } else { "glm-tts" }
        $env:STACKCHAN_TTS_VOICE = if ($env:STACKCHAN_TTS_VOICE) { $env:STACKCHAN_TTS_VOICE } else { "tongtong" }
        $env:STACKCHAN_ASR_COMMAND = if ($env:STACKCHAN_ASR_COMMAND) { $env:STACKCHAN_ASR_COMMAND } else { "$Python $RootDir\scripts\stackchan_asr_glm.py" }
        $env:STACKCHAN_TTS_COMMAND = if ($env:STACKCHAN_TTS_COMMAND) { $env:STACKCHAN_TTS_COMMAND } else { "$Python $RootDir\scripts\stackchan_tts_glm.py" }
        $env:STACKCHAN_TTS_STREAMING = if ($env:STACKCHAN_TTS_STREAMING) { $env:STACKCHAN_TTS_STREAMING } else { "1" }
        $env:STACKCHAN_TTS_COMMAND_STREAMING = if ($env:STACKCHAN_TTS_COMMAND_STREAMING) { $env:STACKCHAN_TTS_COMMAND_STREAMING } else { "1" }
    }
    if (-not $env:OPENAI_API_KEY) {
        throw "Missing API key for cloud chat provider '$chatProvider'."
    }
    $env:NANOBOT_OPENAI_COMPAT_TIMEOUT_S = if ($env:STACKCHAN_CHAT_TIMEOUT) { $env:STACKCHAN_CHAT_TIMEOUT } else { "20" }
}

if ($env:STACKCHAN_CHAT_MODEL) {
    $runtimeConfig = Join-Path $RunDir "nanobot_config.runtime.json"
    & $Python (Join-Path $RootDir "scripts\build_nanobot_runtime_config.py") `
        $NanobotConfig `
        $runtimeConfig `
        --mode $inferenceMode `
        --chat-model $env:STACKCHAN_CHAT_MODEL `
        --chat-provider $(if ($inferenceMode -eq "local") { "local" } else { $chatProvider }) `
        --thinking $(if ($env:STACKCHAN_CHAT_THINKING) { $env:STACKCHAN_CHAT_THINKING } else { "disabled" }) `
        --local-model $(if ($env:STACKCHAN_LOCAL_CHAT_MODEL) { $env:STACKCHAN_LOCAL_CHAT_MODEL } else { "Qwen3-4B" }) `
        --local-context-tokens $(if ($env:STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS) { $env:STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS } else { "8192" }) `
        --local-max-tokens $(if ($env:STACKCHAN_LOCAL_CHAT_MAX_TOKENS) { $env:STACKCHAN_LOCAL_CHAT_MAX_TOKENS } else { "256" }) `
        --local-max-messages $(if ($env:STACKCHAN_LOCAL_MAX_MESSAGES) { $env:STACKCHAN_LOCAL_MAX_MESSAGES } else { "8" })
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
    $NanobotConfig = $runtimeConfig
}

if ($ConfigOnly) {
    Write-Host "Inference mode   : $inferenceMode"
    Write-Host "Public host      : $env:STACKCHAN_PUBLIC_HOST"
    Write-Host "Bridge port      : $BridgePort"
    Write-Host "Nanobot config   : $NanobotConfig"
    exit 0
}

if ($RestartBridge) {
    Stop-PidFile "stackchan-bridge"
    Stop-PortProcess $BridgePort
    Stop-PortProcess $McpPort
}

$healthUrl = "http://127.0.0.1:$BridgePort/health"
$expectedWsUrl = "ws://$($env:STACKCHAN_PUBLIC_HOST):$BridgePort/ws"
$healthCode = Test-Http $healthUrl
if ($healthCode -ne 0) {
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        if ($health.ws_url -ne $expectedWsUrl) {
            Stop-PidFile "stackchan-bridge"
            Stop-PortProcess $BridgePort
            Stop-PortProcess $McpPort
            $healthCode = 0
        }
    } catch {
        $healthCode = 0
    }
}

if ($healthCode -eq 0) {
    $logPath = Join-Path $RunDir "stackchan-bridge.log"
    $errPath = Join-Path $RunDir "stackchan-bridge.err.log"
    $pidPath = Join-Path $RunDir "stackchan-bridge.pid"
    $args = @(
        "-u",
        (Join-Path $RootDir "nanobot_bridge\server.py"),
        "--host", "0.0.0.0",
        "--port", "$BridgePort",
        "--public-host", $env:STACKCHAN_PUBLIC_HOST,
        "--nanobot-config", $NanobotConfig
    )
    $process = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $RootDir -RedirectStandardOutput $logPath -RedirectStandardError $errPath -PassThru -WindowStyle Hidden
    Set-Content -Path $pidPath -Value $process.Id
    Write-Host "stackchan-bridge pid=$($process.Id)"
} else {
    Write-Host "stackchan-bridge already responding: $healthUrl (http $healthCode)"
}

for ($i = 0; $i -lt 30; $i++) {
    if ((Test-Http $healthUrl) -ne 0) {
        break
    }
    Start-Sleep -Milliseconds 500
}

Write-Host ""
Write-Host "Bridge health:"
try {
    Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2 | ConvertTo-Json -Compress
} catch {
    Write-Host "{}"
}
Write-Host ""
Write-Host "StackChan OTA URL: http://$($env:STACKCHAN_PUBLIC_HOST):$BridgePort/xiaozhi/ota/"
Write-Host "StackChan WS URL : ws://$($env:STACKCHAN_PUBLIC_HOST):$BridgePort/ws"
Write-Host "Local MCP URL    : http://127.0.0.1:$McpPort/mcp"
Write-Host "Inference mode   : $inferenceMode"
Write-Host "Chat provider    : $(if ($inferenceMode -eq 'local') { 'local' } else { $chatProvider })"
Write-Host "Chat model       : $($env:STACKCHAN_CHAT_MODEL)"
