# Keeps the Cloudflare tunnel and the demo utility running outside any terminal.
# The Steady app stays under your control: start and stop python -m backend.app yourself.
# Windows starts the tunnel at sign-in and again if that process exits.
# A powered-off or sleeping PC cannot hold the tunnel; it comes back at the next sign-in.
param([switch]$Install, [switch]$ResetDemo)

$ErrorActionPreference = "Stop"
$Root = "C:\Projects\steady\steady"
$LogDir = Join-Path $Root "work\keep-alive"
$Cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BillerTask = "SteadyBiller"
$TunnelTask = "SteadyTunnel"
$WatchTask = "SteadyKeepAlive"

function Test-Listening([int]$Port) {
    $lines = netstat -ano | Select-String ":$Port\s+.*LISTENING"
    return [bool]$lines
}

function Get-TunnelProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
        Where-Object { $_.CommandLine -match "tunnel" -and $_.CommandLine -match "127\.0\.0\.1:5000" }
}

function New-BackgroundSettings {
    New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
}

function Register-BackgroundTask($Name, $Execute, $Arguments, $Description) {
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-BackgroundSettings
    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $Description -Force | Out-Null
}

if ($Install) {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    if (-not (Test-Path $Cloudflared)) { throw "cloudflared was not found at $Cloudflared" }
    if (-not (Test-Path $Python)) { throw "The app Python was not found at $Python" }

    $billerLog = Join-Path $LogDir "biller.log"
    $tunnelLog = Join-Path $LogDir "tunnel.log"
    $billerArgs = "/c `"`"$Python`" -m biller_simulator >> `"$billerLog`" 2>&1`""
    Register-BackgroundTask $BillerTask "cmd.exe" $billerArgs "Demo utility on port 5011, without a terminal."
    $tunnelArgs = "/c `"`"$Cloudflared`" tunnel --url http://127.0.0.1:5000 --no-autoupdate >> `"$tunnelLog`" 2>&1`""
    Register-BackgroundTask $TunnelTask "cmd.exe" $tunnelArgs "Cloudflare tunnel to the Steady app, without a terminal."

    $watchArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    $watch = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $watchArgs -WorkingDirectory $Root
    $user = "$env:USERDOMAIN\$env:USERNAME"
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2) -RepetitionDuration (New-TimeSpan -Days 9999)
    $watchSettings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $WatchTask -Action $watch -Trigger @($atLogon, $repeat) -Principal $principal -Settings $watchSettings -Description "Brings the demo utility and the Cloudflare tunnel back if one has stopped." -Force | Out-Null
    Unregister-ScheduledTask -TaskName "SteadyApp" -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "Installed $BillerTask, $TunnelTask, and $WatchTask."
    return
}

function Stop-TaskQuiet($Name) {
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($task -and $task.State -eq "Running") { Stop-ScheduledTask -TaskName $Name }
}

if ($ResetDemo) {
    # Stop the processes first. Deleting the files while they are open leaves the paid bill in place.
    Stop-TaskQuiet $WatchTask
    Stop-TaskQuiet $BillerTask
    $holders = Get-CimInstance Win32_Process | Where-Object {
        $_.CommandLine -match "-m backend\.app" -or $_.CommandLine -match "-m biller_simulator"
    }
    foreach ($proc in $holders) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Test-Listening 5000) -and -not (Test-Listening 5011)) { break }
        Start-Sleep -Seconds 1
    }
    if ((Test-Listening 5000) -or (Test-Listening 5011)) { throw "The app or utility is still running, so the demo was not reset." }
    Remove-Item -Force (Join-Path $Root "work\external-payments.sqlite3"), (Join-Path $Root "work\external-biller.sqlite3") -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $BillerTask
    for ($i = 0; $i -lt 20; $i++) {
        try {
            $health = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:5011/healthz -TimeoutSec 2
            if ($health.StatusCode -eq 200) { break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not (Test-Listening 5011)) { throw "The demo utility did not start." }
    Start-ScheduledTask -TaskName $WatchTask
    Write-Output "Demo reset. Austin Energy is unpaid again. Start the Steady app yourself."
    return
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Start-IfStopped($Name, $Running) {
    if ($Running) { return }
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return }
    if ($task.State -eq "Running") { Stop-ScheduledTask -TaskName $Name }
    Start-ScheduledTask -TaskName $Name
}

Start-IfStopped $BillerTask (Test-Listening 5011)
Start-IfStopped $TunnelTask ([bool](Get-TunnelProcesses))

$urlFile = Join-Path $LogDir "tunnel-url.txt"
$tunnelLog = Join-Path $LogDir "tunnel.log"
if (-not (Test-Path $tunnelLog)) { return }
$match = Select-String -Path $tunnelLog -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" | Select-Object -Last 1
if (-not $match) { return }
$url = $match.Matches[0].Value
$previous = ""
if (Test-Path $urlFile) { $previous = (Get-Content $urlFile -Raw).Trim() }
if ($url -eq $previous) { return }
Set-Content -Path $urlFile -Value $url -Encoding ascii
& $Python -m backend.register_webhook $url *> (Join-Path $LogDir "webhook.txt")
exit 0
