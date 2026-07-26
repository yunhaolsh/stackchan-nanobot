param(
    [string]$RuntimeDir = "",
    [string]$ModelRoot = "",
    [string]$Python = "",
    [string]$GpuBackend = "cpu",
    [switch]$SkipLlmBuild,
    [switch]$SkipSpeech
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $RuntimeDir) { $RuntimeDir = if ($env:STACKCHAN_LOCAL_RUNTIME_DIR) { $env:STACKCHAN_LOCAL_RUNTIME_DIR } else { Join-Path $RootDir ".local-runtime" } }
if (-not $ModelRoot) { $ModelRoot = if ($env:STACKCHAN_LOCAL_MODEL_ROOT) { $env:STACKCHAN_LOCAL_MODEL_ROOT } else { Join-Path $RootDir "models" } }
if (-not $Python) { $Python = if ($env:STACKCHAN_LOCAL_PYTHON) { $env:STACKCHAN_LOCAL_PYTHON } else { "py -3.12" } }

$LlamaSource = Join-Path $RuntimeDir "llama.cpp"
$LlamaBuild = if ($env:STACKCHAN_LLAMA_BUILD_DIR) { $env:STACKCHAN_LLAMA_BUILD_DIR } else { Join-Path $LlamaSource "build-stackchan" }
$LlamaRef = if ($env:STACKCHAN_LLAMA_CPP_REF) { $env:STACKCHAN_LLAMA_CPP_REF } else { "b9631" }
$LlamaCommit = if ($env:STACKCHAN_LLAMA_CPP_COMMIT) { $env:STACKCHAN_LLAMA_CPP_COMMIT } else { "6e14286edaa60a223292c8a996506905b2f66f66" }

$LlmModelDir = if ($env:STACKCHAN_LOCAL_LLM_MODEL_DIR) { $env:STACKCHAN_LOCAL_LLM_MODEL_DIR } else { Join-Path $ModelRoot "llm" }
$LlmModelName = "Qwen3-4B-Q4_K_M.gguf"
$LlmModelPath = if ($env:STACKCHAN_LLAMA_MODEL_PATH) { $env:STACKCHAN_LLAMA_MODEL_PATH } else { Join-Path $LlmModelDir $LlmModelName }
$LlmRevision = if ($env:STACKCHAN_LLAMA_MODEL_REVISION) { $env:STACKCHAN_LLAMA_MODEL_REVISION } else { "bc640142c66e1fdd12af0bd68f40445458f3869b" }
$LlmModelUrl = if ($env:STACKCHAN_LLAMA_MODEL_URL) { $env:STACKCHAN_LLAMA_MODEL_URL } else { "https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/$LlmRevision/$LlmModelName`?download=true" }
$LlmSha256 = if ($env:STACKCHAN_LLAMA_MODEL_SHA256) { $env:STACKCHAN_LLAMA_MODEL_SHA256 } else { "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5" }

$SpeechVenv = if ($env:STACKCHAN_LOCAL_SPEECH_VENV) { $env:STACKCHAN_LOCAL_SPEECH_VENV } else { Join-Path $RootDir ".venv-local-speech" }
$SpeechModelRoot = if ($env:STACKCHAN_LOCAL_SPEECH_MODEL_ROOT) { $env:STACKCHAN_LOCAL_SPEECH_MODEL_ROOT } else { Join-Path $ModelRoot "speech" }

function Invoke-Python {
    param([string[]]$Args)
    if ($Python.Contains(" ")) {
        $parts = $Python.Split(" ", 2)
        & $parts[0] $parts[1] @Args
    } else {
        & $Python @Args
    }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Download-FileChecked {
    param(
        [string]$Url,
        [string]$Path,
        [string]$Sha256
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    if (-not (Test-Path $Path)) {
        $part = "$Path.part"
        Write-Host "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $part
        Move-Item -Force $part $Path
    }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $Path).Hash.ToLowerInvariant()
    if ($actual -ne $Sha256.ToLowerInvariant()) {
        throw "SHA256 mismatch for $Path. expected=$Sha256 actual=$actual"
    }
}

function Download-TarBz2Model {
    param(
        [string]$Url,
        [string]$Directory,
        [string]$RequiredFile,
        [string]$RequiredSha256
    )
    $targetFile = Join-Path (Join-Path $SpeechModelRoot $Directory) $RequiredFile
    if (Test-Path $targetFile) {
        $actual = (Get-FileHash -Algorithm SHA256 -Path $targetFile).Hash.ToLowerInvariant()
        if ($actual -ne $RequiredSha256.ToLowerInvariant()) {
            throw "SHA256 mismatch for $targetFile. expected=$RequiredSha256 actual=$actual"
        }
        Write-Host "Model already present: $Directory"
        return
    }
    New-Item -ItemType Directory -Force -Path $SpeechModelRoot | Out-Null
    $archive = Join-Path $SpeechModelRoot "$Directory.tar.bz2"
    if (-not (Test-Path $archive)) {
        Write-Host "Downloading $Url"
        Invoke-WebRequest -Uri $Url -OutFile $archive
    }
    if (-not (Get-Command tar -ErrorAction SilentlyContinue)) {
        throw "Windows tar is required to extract $archive"
    }
    tar -xjf $archive -C $SpeechModelRoot
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $targetFile).Hash.ToLowerInvariant()
    if ($actual -ne $RequiredSha256.ToLowerInvariant()) {
        throw "SHA256 mismatch for $targetFile. expected=$RequiredSha256 actual=$actual"
    }
}

New-Item -ItemType Directory -Force -Path $RuntimeDir, $ModelRoot | Out-Null

if (-not $SkipLlmBuild) {
    if (-not (Test-Path (Join-Path $LlamaSource ".git"))) {
        git clone --depth 1 --branch $LlamaRef https://github.com/ggml-org/llama.cpp.git $LlamaSource
    } else {
        git -C $LlamaSource fetch --depth 1 origin $LlamaRef
        git -C $LlamaSource checkout --detach FETCH_HEAD
    }
    if ($LlamaCommit) {
        $head = git -C $LlamaSource rev-parse HEAD
        if ($head -ne $LlamaCommit) {
            throw "Unexpected llama.cpp commit: $head"
        }
    }

    $cmakeArgs = @(
        "-DGGML_NATIVE=ON",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_APP=OFF",
        "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_BUILD_UI=OFF",
        "-DLLAMA_USE_PREBUILT_UI=OFF"
    )
    if ($GpuBackend -eq "cuda") {
        $cmakeArgs += "-DGGML_CUDA=ON"
    } elseif ($GpuBackend -eq "vulkan") {
        $cmakeArgs += "-DGGML_VULKAN=ON"
    } else {
        $cmakeArgs += "-DGGML_CUDA=OFF"
    }
    cmake -S $LlamaSource -B $LlamaBuild @cmakeArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmake --build $LlamaBuild --config Release --target llama-server
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Download-FileChecked -Url $LlmModelUrl -Path $LlmModelPath -Sha256 $LlmSha256
Write-Host "Local LLM model is ready: $LlmModelPath"

if (-not $SkipSpeech) {
    Invoke-Python -Args @("-m", "venv", $SpeechVenv)
    $SpeechPython = Join-Path $SpeechVenv "Scripts\python.exe"
    $SpeechPip = Join-Path $SpeechVenv "Scripts\pip.exe"
    & $SpeechPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $SpeechPip install -r (Join-Path $RootDir "requirements-local-speech.txt")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

    Download-TarBz2Model `
        -Url "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2" `
        -Directory "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17" `
        -RequiredFile "model.int8.onnx" `
        -RequiredSha256 "c71f0ce00bec95b07744e116345e33d8cbbe08cef896382cf907bf4b51a2cd51"

    Download-TarBz2Model `
        -Url "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2" `
        -Directory "vits-melo-tts-zh_en" `
        -RequiredFile "model.onnx" `
        -RequiredSha256 "bf30582eb1b012250a35b1a4a80e7dfbcf8485e7bb9de0d95efbbeef0e4ad86d"

    & $SpeechPython (Join-Path $RootDir "local_inference\speech_service.py") --check
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "Local speech runtime is ready."
}
