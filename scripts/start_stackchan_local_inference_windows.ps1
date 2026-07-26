param(
    [int]$LlamaPort = 18080,
    [int]$SpeechPort = 18081,
    [string]$LlamaServer = "",
    [string]$LlamaModel = "",
    [string]$SpeechPython = "",
    [int]$LlamaThreads = 0,
    [int]$LlamaGpuLayers = 0
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunDir = Join-Path $RootDir ".run"
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

if (-not $SpeechPython) { $SpeechPython = if ($env:STACKCHAN_LOCAL_SPEECH_PYTHON) { $env:STACKCHAN_LOCAL_SPEECH_PYTHON } else { Join-Path $RootDir ".venv-local-speech\Scripts\python.exe" } }
if (-not $LlamaServer) {
    $defaultExe = Join-Path $RootDir ".local-runtime\llama.cpp\build-stackchan\bin\Release\llama-server.exe"
    $altExe = Join-Path $RootDir ".local-runtime\llama.cpp\build-stackchan\bin\llama-server.exe"
    $LlamaServer = if (Test-Path $defaultExe) { $defaultExe } else { $altExe }
}
if (-not $LlamaModel) { $LlamaModel = if ($env:STACKCHAN_LLAMA_MODEL_PATH) { $env:STACKCHAN_LLAMA_MODEL_PATH } else { Join-Path $RootDir "models\llm\Qwen3-4B-Q4_K_M.gguf" } }
if ($LlamaThreads -le 0) { $LlamaThreads = if ($env:STACKCHAN_LLAMA_THREADS) { [int]$env:STACKCHAN_LLAMA_THREADS } else { [Math]::Max(2, [Environment]::ProcessorCount - 2) } }
if ($LlamaGpuLayers -eq 0 -and $env:STACKCHAN_LLAMA_GPU_LAYERS) { $LlamaGpuLayers = [int]$env:STACKCHAN_LLAMA_GPU_LAYERS }

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

function Start-IfNeeded {
    param(
        [string]$Name,
        [string]$ProbeUrl,
        [string]$Exe,
        [string[]]$Args
    )
    $code = Test-Http $ProbeUrl
    if ($code -ge 200 -and $code -lt 300) {
        Write-Host "$Name already healthy: $ProbeUrl"
        return
    }
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (Test-Path $pidFile) {
        $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
            Write-Host "$Name already running: pid=$oldPid"
            return
        }
    }
    $logPath = Join-Path $RunDir "$Name.log"
    $errPath = Join-Path $RunDir "$Name.err.log"
    Write-Host "Starting $Name..."
    $process = Start-Process -FilePath $Exe -ArgumentList $Args -WorkingDirectory $RootDir -RedirectStandardOutput $logPath -RedirectStandardError $errPath -PassThru -WindowStyle Hidden
    Set-Content -Path $pidFile -Value $process.Id
    Write-Host "$Name pid=$($process.Id)"
}

function Wait-Healthy {
    param(
        [string]$Name,
        [string]$Url,
        [int]$TimeoutSeconds
    )
    for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
        $code = Test-Http $Url
        if ($code -ge 200 -and $code -lt 300) {
            Write-Host "$Name ready: $Url"
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "$Name did not become healthy within ${TimeoutSeconds}s. Inspect .run\$Name.log"
}

if (-not (Test-Path $SpeechPython)) {
    throw "Missing local speech environment. Run scripts\setup_stackchan_local_inference_windows.ps1 first."
}
if (-not (Test-Path $LlamaServer)) {
    throw "Missing llama-server. Run scripts\setup_stackchan_local_inference_windows.ps1 first."
}
if (-not (Test-Path $LlamaModel)) {
    throw "Missing local LLM model: $LlamaModel"
}

Start-IfNeeded `
    -Name "stackchan-local-speech" `
    -ProbeUrl "http://127.0.0.1:$SpeechPort/health" `
    -Exe $SpeechPython `
    -Args @("-u", (Join-Path $RootDir "local_inference\speech_service.py"), "--host", "127.0.0.1", "--port", "$SpeechPort")

$llamaArgs = @(
    "--host", "127.0.0.1",
    "--port", "$LlamaPort",
    "-m", $LlamaModel,
    "--alias", $(if ($env:STACKCHAN_LOCAL_CHAT_MODEL) { $env:STACKCHAN_LOCAL_CHAT_MODEL } else { "Qwen3-4B" }),
    "-c", $(if ($env:STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS) { $env:STACKCHAN_LOCAL_CHAT_CONTEXT_TOKENS } else { "8192" }),
    "-n", $(if ($env:STACKCHAN_LOCAL_CHAT_MAX_TOKENS) { $env:STACKCHAN_LOCAL_CHAT_MAX_TOKENS } else { "256" }),
    "-t", "$LlamaThreads",
    "-np", $(if ($env:STACKCHAN_LLAMA_PARALLEL) { $env:STACKCHAN_LLAMA_PARALLEL } else { "1" }),
    "--cache-ram", $(if ($env:STACKCHAN_LLAMA_PROMPT_CACHE_MB) { $env:STACKCHAN_LLAMA_PROMPT_CACHE_MB } else { "512" }),
    "--jinja",
    "--reasoning", "off",
    "--offline",
    "--no-ui"
)
if ($LlamaGpuLayers -ne 0) {
    $llamaArgs += @("-ngl", "$LlamaGpuLayers")
}

Start-IfNeeded `
    -Name "stackchan-local-llm" `
    -ProbeUrl "http://127.0.0.1:$LlamaPort/health" `
    -Exe $LlamaServer `
    -Args $llamaArgs

Wait-Healthy -Name "stackchan-local-speech" -Url "http://127.0.0.1:$SpeechPort/health" -TimeoutSeconds $(if ($env:STACKCHAN_LOCAL_SPEECH_START_TIMEOUT_S) { [int]$env:STACKCHAN_LOCAL_SPEECH_START_TIMEOUT_S } else { 120 })
Wait-Healthy -Name "stackchan-local-llm" -Url "http://127.0.0.1:$LlamaPort/health" -TimeoutSeconds $(if ($env:STACKCHAN_LOCAL_LLM_START_TIMEOUT_S) { [int]$env:STACKCHAN_LOCAL_LLM_START_TIMEOUT_S } else { 900 })
