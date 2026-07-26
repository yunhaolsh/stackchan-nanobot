param(
    [int]$LlamaPort = 18080,
    [int]$SpeechPort = 18081
)

$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$RunDir = Join-Path $RootDir ".run"

function Stop-PidFile {
    param([string]$Name)
    $pidFile = Join-Path $RunDir "$Name.pid"
    if (-not (Test-Path $pidFile)) {
        Write-Host "$Name not tracked"
        return
    }
    $oldPid = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

function Stop-PortProcess {
    param(
        [string]$Name,
        [int]$Port
    )
    $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($connection in $connections) {
        if ($connection.OwningProcess -and $connection.OwningProcess -ne $PID) {
            Write-Host "Stopping untracked $Name on port $Port pid=$($connection.OwningProcess)"
            Stop-Process -Id $connection.OwningProcess -Force -ErrorAction SilentlyContinue
        }
    }
}

Stop-PidFile "stackchan-local-speech"
Stop-PidFile "stackchan-local-llm"
Stop-PortProcess "stackchan-local-speech" $SpeechPort
Stop-PortProcess "stackchan-local-llm" $LlamaPort
