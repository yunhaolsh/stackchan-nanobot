param(
    [int]$BridgePort = 12800,
    [int]$McpPort = 12801,
    [int]$NanobotApiPort = 8900
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
    $oldPid = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "Stopping $Name pid=$oldPid"
            Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
        } else {
            Write-Host "$Name pid not running"
        }
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

Stop-PidFile "stackchan-bridge"
Stop-PidFile "nanobot-api"

Stop-PortProcess "stackchan-bridge" $BridgePort
Stop-PortProcess "stackchan-mcp" $McpPort
Stop-PortProcess "nanobot-api" $NanobotApiPort
