param([int]$Port = 8766, [string]$LanAddress = '', [switch]$Restart, [switch]$EnablePrivateFirewall, [string]$Python = '')
$ErrorActionPreference = 'Stop'
$projectDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $PSScriptRoot 'python-runtime.ps1')
$pythonExecutable = Resolve-ProjectPython $Python
$stateDirectory = Join-Path $projectDirectory '.local'
New-Item -ItemType Directory -Force -Path $stateDirectory | Out-Null
$listenAddress='127.0.0.1'
if ($LanAddress) {
    if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $LanAddress -ErrorAction SilentlyContinue)) { throw 'LanAddress must be an IPv4 address assigned to this computer.' }
    $listenAddress='0.0.0.0'
    $env:XIAOYUN_LAN_HOSTS=$LanAddress
    if ($EnablePrivateFirewall) {
        $ruleName="Xiaoyun-ESP32-$Port"
        if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -Name $ruleName -DisplayName "Xiaoyun ESP32 $Port (private subnet)" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -LocalAddress $LanAddress -RemoteAddress LocalSubnet -Profile Private -Program $pythonExecutable | Out-Null
        } else {
            Get-NetFirewallRule -Name $ruleName | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -LocalAddress $LanAddress -RemoteAddress LocalSubnet | Out-Null
        }
    }
} else { Remove-Item Env:XIAOYUN_LAN_HOSTS -ErrorAction SilentlyContinue }
$runningService=$false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
    $runningService=$health.service -eq 'xiaoyun-realtime'
} catch {}
if ($runningService) {
    if (-not $Restart) { Write-Output "Already running: http://127.0.0.1:$Port (use -Restart to reload code or change listening address)"; exit 0 }
    $listeners=Get-NetTCPConnection -State Listen -LocalPort $Port
    foreach($serverPid in ($listeners.OwningProcess | Select-Object -Unique)) {
        $runningProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$serverPid"
        if ($runningProcess.CommandLine -notmatch 'uvicorn.*xiaoyun\.app:app') { throw 'Listener is not the expected Xiaoyun server.' }
        Stop-Process -Id $serverPid
    }
    Start-Sleep -Milliseconds 300
}
$serverProcess = Start-Process -FilePath $pythonExecutable -ArgumentList '-m','uvicorn','xiaoyun.app:app','--host',$listenAddress,'--port',"$Port" -WorkingDirectory $projectDirectory -WindowStyle Hidden -RedirectStandardOutput (Join-Path $stateDirectory 'server.stdout.log') -RedirectStandardError (Join-Path $stateDirectory 'server.stderr.log') -PassThru
$serverProcess.Id | Set-Content -LiteralPath (Join-Path $stateDirectory 'server.pid')
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 1
        if ($health.service -eq 'xiaoyun-realtime') { Write-Output "Ready: http://127.0.0.1:$Port"; exit 0 }
    } catch {}
}
throw 'Server did not start. Read .local/server.stderr.log.'
