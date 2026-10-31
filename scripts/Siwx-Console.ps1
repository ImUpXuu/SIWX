#Requires -Version 5.1
# One-command Windows entry; no Python installation required.
# Relay adapted from abxian/wexport1/scripts/Wx-Console.ps1 (v3.4).
& {
    $ErrorActionPreference = 'Stop'
    $bridgeDirectory = $null
    $currentTask = $null
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        if ($env:OS -ne 'Windows_NT') { throw 'Windows PowerShell 5.1 or newer is required.' }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $admin = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $identity.IsSystem -and -not $admin.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'Run from SYSTEM or an elevated PowerShell console, as with wexport1.'
        }
        Import-Module ScheduledTasks
        $scriptHost = "$env:SystemRoot\System32\wscript.exe"
        if (-not (Test-Path -LiteralPath $scriptHost -PathType Leaf)) { throw 'Windows Script Host is unavailable.' }
        Write-Host 'SIWX console v1.0 - runs as the selected logged-on desktop user; no browser auto-open.'
        $suggestedUser = (Get-CimInstance Win32_ComputerSystem).UserName
        $userId = Read-Host "Target Windows user (DOMAIN\User), Enter = [$suggestedUser]"
        if (-not $userId) { $userId = $suggestedUser }
        if (-not $userId) { throw 'Log on to Windows first.' }
        $sid = (New-Object Security.Principal.NTAccount($userId)).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) { throw 'Select a desktop user, not a service account.' }
        $sessions = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | ForEach-Object {
            $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid
            if ($owner.ReturnValue -eq 0 -and $owner.Sid -eq $sid) { $_.SessionId }
        } | Sort-Object -Unique)
        if ($sessions.Count -ne 1) { throw 'The user must have exactly one logged-on Explorer desktop session.' }
        $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" | Select-Object -First 1
        if (-not $profile.LocalPath) { throw 'Target user profile not found.' }

        function Assert-SiwxLocalPath([string]$Path) {
            if ($Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[%"\r\n]') {
                throw 'Use a local drive path without percent signs, quotes, or line breaks.'
            }
            $cursor = [IO.Path]::GetFullPath($Path)
            while ($cursor) {
                if ((Test-Path -LiteralPath $cursor) -and
                    ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    throw 'Refusing a path containing a reparse point.'
                }
                $cursor = Split-Path -Parent $cursor
            }
        }
        function New-SiwxPrivateDirectory([string]$Path, [string]$UserSid) {
            Assert-SiwxLocalPath $Path
            if (-not (Test-Path -LiteralPath $Path)) {
                New-Item -ItemType Directory -Path $Path | Out-Null
                $acl = New-Object Security.AccessControl.DirectorySecurity
                $acl.SetAccessRuleProtection($true, $false)
                foreach ($accessSid in @('S-1-5-18', 'S-1-5-32-544', $UserSid)) {
                    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                        (New-Object Security.Principal.SecurityIdentifier($accessSid)),
                        'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
                    $acl.AddAccessRule($rule)
                }
                Set-Acl -LiteralPath $Path -AclObject $acl
            } else {
                if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw 'Runtime path is not a directory.' }
                # Never silently alter an existing user's ACL. Refuse unexpected grants.
                $acl = Get-Acl -LiteralPath $Path
                $allowed = @('S-1-5-18', 'S-1-5-32-544', $UserSid)
                foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                    if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) {
                        throw "Existing runtime directory grants other users access: $Path. Choose a private directory."
                    }
                }
            }
        }
        $rootPath = Join-Path $profile.LocalPath 'AppData\Local\stories-in-wx-console'
        New-SiwxPrivateDirectory $rootPath $sid
        $exePath = Join-Path $rootPath 'siwx-v5.2.0.exe'
        # Public GitHub Release SHA-256 digest, verified against API on 2026-10-31.
        $expectedHash = 'dcecef343256f8d5c7335da82ea6a0311a0b1b2261a8a7dc763ba7bb7fba8dc6'
        $downloadUrl = 'https://github.com/ImUpXuu/SIWX/releases/download/v5.2.0/stories-in-wx-v5.2.0-windows-x64.exe'
        [Net.ServicePointManager]::SecurityProtocol = $previousTls -bor [Net.SecurityProtocolType]::Tls12
        function Install-SiwxRelease([string]$ExePath, [string]$DownloadUrl, [string]$ExpectedHash) {
            if (-not (Test-Path -LiteralPath $exePath)) {
                $temporary = $exePath + '.' + [Guid]::NewGuid().ToString('N') + '.download'
                try {
                    Write-Host 'Downloading SIWX v5.2.0 Windows x64 (about 19 MB)...'
                    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $temporary -TimeoutSec 300
                    if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $expectedHash) {
                        throw 'Release SHA-256 mismatch; nothing will be started.'
                    }
                    Move-Item -LiteralPath $temporary -Destination $exePath
                } finally {
                    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
                }
            }
            if ((Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash -ne $expectedHash) {
                throw 'Existing EXE differs from the pinned release; retained without executing or overwriting it.'
            }
        }
        Install-SiwxRelease $exePath $downloadUrl $expectedHash
        Assert-SiwxLocalPath $exePath
        Write-Host "Runtime/output directory: $rootPath"
        Write-Host 'Key cache uses the target user LocalAppData. Keep decrypted files and logs private.'
        $bridgeDirectory = Join-Path "$env:SystemRoot\Temp" ('SiwxBridge-' + [Guid]::NewGuid().ToString('N'))
        New-SiwxPrivateDirectory $bridgeDirectory $sid

        function Test-SiwxPort {
            $client = New-Object Net.Sockets.TcpClient
            try {
                $async = $client.BeginConnect('127.0.0.1', 8787, $null, $null)
                if (-not $async.AsyncWaitHandle.WaitOne(250)) { return $false }
                $client.EndConnect($async)
                return $true
            } catch { return $false } finally { $client.Dispose() }
        }
        function Test-SiwxHttp {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8787/' -TimeoutSec 2
                return $response.StatusCode -eq 200 -and $response.Content -match 'stories-in-wx|SIWX'
            } catch { return $false }
        }
        function Get-SiwxBackgroundTasks([string]$UserSid) {
            $root = [IO.Path]::GetFullPath((Join-Path $env:SystemRoot 'Temp')).TrimEnd('\', '/')
            $separator = [string][IO.Path]::DirectorySeparatorChar
            $pattern = '^' + [regex]::Escape($root + $separator) + 'SiwxBridge-[a-f0-9]{32}' +
                [regex]::Escape($separator) + '[a-f0-9]{32}\.log$'
            foreach ($task in @(Get-ScheduledTask -TaskName 'SiwxBackground-*' -ErrorAction SilentlyContinue)) {
                try {
                    $meta = $task.Description | ConvertFrom-Json -ErrorAction Stop
                    if ($meta.Kind -ne 'SiwxConsoleBackground' -or $meta.UserSid -ne $UserSid) { continue }
                    $principalSid = $task.Principal.UserId
                    if ($principalSid -notmatch '^S-1-') {
                        $principalSid = (New-Object Security.Principal.NTAccount($principalSid)).Translate(
                            [Security.Principal.SecurityIdentifier]).Value
                    }
                    if ($principalSid -ne $UserSid) { continue }
                    $log = [IO.Path]::GetFullPath($meta.LogPath)
                    if ($log -notmatch $pattern) { continue }
                    if ($task.TaskName -ne ('SiwxBackground-' + [IO.Path]::GetFileNameWithoutExtension($log)) -or
                        $task.TaskPath -ne '\') { continue }
                    $launcherPath = [IO.Path]::ChangeExtension($log, 'js')
                    if (@($task.Actions).Count -ne 1 -or $task.Actions[0].Execute -ne $scriptHost -or
                        $task.Actions[0].Arguments -ne ('//B //Nologo //E:JScript "' + $launcherPath + '"')) { continue }
                            $directory = Get-Item -LiteralPath (Split-Path -Parent $log) -ErrorAction Stop
                    if ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
                    $unsafe = $false
                    foreach ($path in @($log, $launcherPath, [IO.Path]::ChangeExtension($log, 'cmd'))) {
                        if ((Test-Path -LiteralPath $path) -and
                            ((Get-Item -LiteralPath $path).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                            $unsafe = $true
                        }
                    }
                    if ($unsafe) { continue }
                    [pscustomobject]@{
                        Task = $task; LogPath = $log
                    }
                } catch { continue }
            }
        }

        function New-SiwxCmdWorker(
            [string]$ExePath, [string]$Sid, [string]$LogPath,
            [string]$ResultPath, [string]$ArgumentLine, [bool]$Probe, [string]$RootPath
        ) {
            foreach ($path in @($ExePath, $LogPath, $ResultPath)) {
                if ($path -match '[%"\r\n]') {
                    throw 'CMD worker paths cannot contain percent signs, quotes, or line breaks.'
                }
            }
            if ($Sid -notmatch '^S-1-[0-9-]+$') { throw 'Invalid user SID.' }
            if ($ArgumentLine -notmatch '^(doctor|auto|keys extract|keys list|decrypt|serve --host 127\.0\.0\.1 --port 8787 --no-open|probe)$') {
                throw 'Unsupported command arguments.'
            }
            if (-not $RootPath -or $RootPath -match '[%"\r\n]') { throw 'Unsafe runtime root.' }
            $directory = Split-Path -Parent $ExePath
            $probeLine = 'rem Not a probe'
            if ($Probe) { $probeLine = 'goto probe_success' }
            # Disable ! expansion; reject % expansion. All paths are quoted.
            $template = @'
@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "SIWX_ROOT={ROOT}"
if not exist "{ROOT}" goto path_error
>>"{LOG}" echo Stage: CMD worker started
>>"{LOG}" echo Actual user:
"%SystemRoot%\System32\whoami.exe" >>"{LOG}" 2>&1
"%SystemRoot%\System32\whoami.exe" /user /fo csv /nh >"{LOG}.sid" 2>>"{LOG}"
if errorlevel 1 goto identity_error
set "ACTUAL_SID="
for /f "usebackq tokens=2 delims=," %%S in ("{LOG}.sid") do set "ACTUAL_SID=%%~S"
if not "%ACTUAL_SID%"=="{SID}" goto identity_error
{PROBE}
cd /d "{DIR}" >>"{LOG}" 2>&1
if errorlevel 1 goto path_error
if not exist "{EXE}" goto path_error
>>"{LOG}" echo Command: "{EXE}" {ARGS}
>>"{LOG}" echo Stage: invoking siwx.exe
"{EXE}" {ARGS} <nul >>"{LOG}" 2>&1
set "WX_EXIT=%errorlevel%"
>>"{LOG}" echo Stage: siwx.exe returned, exit code %WX_EXIT%
goto report
:probe_success
>>"{LOG}" echo Stage: user-session CMD probe succeeded
set "WX_EXIT=0"
goto report
:identity_error
>>"{LOG}" echo ERROR: unexpected user SID; refusing to run siwx.exe
set "WX_EXIT=5"
goto report
:path_error
>>"{LOG}" echo ERROR: target user cannot access siwx.exe or its directory
set "WX_EXIT=3"
:report
>"{RESULT}.tmp" echo %WX_EXIT%
move /y "{RESULT}.tmp" "{RESULT}" >nul
exit /b %WX_EXIT%
'@
            # Single-pass expansion: braces in a path cannot become a later template placeholder.
            $values = @{
                LOG = $LogPath; SID = $Sid; PROBE = $probeLine; DIR = $directory
                EXE = $ExePath; ARGS = $ArgumentLine; RESULT = $ResultPath; ROOT = $RootPath
            }
            $expanded = [regex]::Replace($template, '\{(LOG|SID|PROBE|DIR|EXE|ARGS|RESULT|ROOT)\}', {
                param($match)
                $values[$match.Groups[1].Value]
            })
            return $expanded.Replace("`r`n", "`n").Replace("`n", "`r`n") + "`r`n"
        }

        function New-SiwxHiddenLauncher([string]$WorkerPath, [string]$LogPath, [string]$ResultPath) {
            $command = '"' + "$env:SystemRoot\System32\cmd.exe" + '" /d /s /c ""' + $WorkerPath + '""'
            $commandJson = ConvertTo-Json -InputObject $command -Compress
            $logJson = ConvertTo-Json -InputObject $LogPath -Compress
            $resultJson = ConvertTo-Json -InputObject $ResultPath -Compress
            return @"
var fs = new ActiveXObject("Scripting.FileSystemObject");
var sh = new ActiveXObject("WScript.Shell");
var logPath = $logJson;
var resultPath = $resultJson;
function log(message) {
    var f = fs.OpenTextFile(logPath, 8, true, 0);
    f.WriteLine(message);
    f.Close();
}
function failedResult() {
    if (!fs.FileExists(resultPath)) {
        var f = fs.CreateTextFile(resultPath + ".tmp", true, false);
        f.WriteLine("1");
        f.Close();
        fs.MoveFile(resultPath + ".tmp", resultPath);
    }
}
try {
    log("Stage: hidden launcher started");
    var code = sh.Run($commandJson, 0, true);
    if (!fs.FileExists(resultPath)) {
        log("ERROR: CMD returned without a result file. Host exit code: " + code);
        failedResult();
        code = 1;
    }
    WScript.Quit(code);
} catch (e) {
    log("ERROR: hidden launcher failed: " + e.number + " " + e.description);
    failedResult();
    WScript.Quit(1);
}
"@
        }

        function Show-SiwxTaskEvents([string]$TaskName, [datetime]$Since) {
            try {
                $events = Get-WinEvent -FilterHashtable @{
                    LogName = 'Microsoft-Windows-TaskScheduler/Operational'
                    StartTime = $Since.AddSeconds(-2)
                    Id = @(100, 101, 102, 111, 129, 200, 201, 203)
                } -MaxEvents 200 -ErrorAction Stop
                $matching = @($events | Where-Object {
                    [xml]$xml = $_.ToXml()
                    @($xml.Event.EventData.Data | Where-Object {
                        $_.Name -eq 'TaskName' -and $_.InnerText.TrimStart('\') -eq $TaskName
                    }).Count -gt 0
                })
                if ($matching.Count -eq 0) {
                    Write-Host 'No matching task-history events available; no log settings changed.'
                }
                foreach ($event in $matching | Sort-Object TimeCreated) {
                    Write-Host ("Task event {0} {1}: {2}" -f $event.TimeCreated, $event.Id, $event.Message)
                }
            } catch {
                Write-Host 'Task history unavailable or disabled; no log settings changed.'
            }
        }

        function Show-SiwxOutput([string]$Path, [ref]$Position, [bool]$Final = $false) {
            if (-not (Test-Path -LiteralPath $Path)) { return }
            $stream = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
            try {
                $null = $stream.Seek($Position.Value, 'Begin')
                $reader = New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)
                try {
                    $text = $reader.ReadToEnd()
                    # Leave an incomplete UTF-8 line for the next read.
                    $end = $text.LastIndexOf("`n")
                    if ($Final -and $text.Length -gt 0) { $end = $text.Length - 1 }
                    if ($end -ge 0) {
                        $complete = $text.Substring(0, $end + 1)
                        Write-Host -NoNewline $complete
                        $Position.Value += [Text.Encoding]::UTF8.GetByteCount($complete)
                    }
                } finally { $reader.Dispose() }
            } finally { $stream.Dispose() }
        }

        $nextCommand = 'p'
        while ($true) {
            if (-not $nextCommand) {
                Write-Host "`n1 = environment check   2 = auto extract + decrypt   3 = extract keys only"
                Write-Host '4 = show masked keys   5 = decrypt using cached keys'
                Write-Host '6 = Web with live logs   7 = start background Web'
                Write-Host '8 = background status / URL / logs   9 = stop background Web   0 = exit'
                $nextCommand = Read-Host 'Choose'
            }
            if ($nextCommand -eq '0') { break }
            if ($nextCommand -in @('8', '9')) {
                $backgroundTasks = @(Get-SiwxBackgroundTasks $sid)
                if ($backgroundTasks.Count -eq 0) { Write-Host 'No managed background tasks for this user.' }
                for ($index = 0; $index -lt $backgroundTasks.Count; $index++) {
                    $record = $backgroundTasks[$index]
                    Write-Host ("[{0}] {1} state={2}" -f ($index + 1), $record.Task.TaskName, $record.Task.State)
                    Write-Host "URL: http://127.0.0.1:8787/ (valid only while the matching service is running)"
                    Write-Host "Log: $($record.LogPath)"
                    if ($nextCommand -eq '8' -and (Test-Path -LiteralPath $record.LogPath)) {
                        Get-Content -LiteralPath $record.LogPath -Encoding UTF8 -Tail 20 | ForEach-Object { Write-Host $_ }
                    }
                }
                if ($nextCommand -eq '9' -and $backgroundTasks.Count -gt 0) {
                    $selection = 0
                    $answer = Read-Host 'Task number to stop (blank = cancel)'
                    if ([int]::TryParse($answer, [ref]$selection) -and $selection -ge 1 -and $selection -le $backgroundTasks.Count) {
                        $selected = $backgroundTasks[$selection - 1].Task
                        Stop-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath
                        for ($wait = 0; $wait -lt 20; $wait++) {
                            $state = (Get-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath).State
                            if ($state -notin @('Running', 'Queued')) { break }
                            Start-Sleep -Milliseconds 100
                        }
                        if ($state -in @('Running', 'Queued')) { throw 'Task has not stopped; retained for inspection.' }
                        Unregister-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath -Confirm:$false
                        Write-Host 'Stopped and unregistered. Diagnostic files retained.'
                    }
                }
                $nextCommand = $null
                continue
            }
            $isProbe = $nextCommand -eq 'p'
            $backgroundMode = $nextCommand -eq '7'
            $isServe = $nextCommand -in @('6', '7')
            # All SIWX operations share the same cache/output root. Avoid concurrent writers.
            if (-not $isProbe -and @(Get-SiwxBackgroundTasks $sid | Where-Object {
                $_.Task.State -in @('Running', 'Queued') }).Count -gt 0) {
                Write-Host 'A managed Web task is active. Use its Web interface, or inspect/stop it using 8/9.'
                $nextCommand = $null
                continue
            }
            $siwxArguments = switch ($nextCommand) {
                'p' { 'probe' }
                '1' { 'doctor' }
                '2' { 'auto' }
                '3' { 'keys extract' }
                '4' { 'keys list' }
                '5' { 'decrypt' }
                { $_ -in @('6', '7') } { 'serve --host 127.0.0.1 --port 8787 --no-open' }
            }
            if (-not $siwxArguments) { $nextCommand = $null; continue }
            if ($isServe -and (Test-SiwxPort)) {
                Write-Host 'Port 8787 is occupied. Stop the existing service first; it will not be replaced.'
                $nextCommand = $null
                continue
            }
            $nextCommand = $null
            Assert-SiwxLocalPath $rootPath
            Assert-SiwxLocalPath $exePath
            $jobId = [Guid]::NewGuid().ToString('N')
            $logPath = Join-Path $bridgeDirectory "$jobId.log"
            $resultPath = Join-Path $bridgeDirectory "$jobId.result"
            $workerPath = Join-Path $bridgeDirectory "$jobId.cmd"
            $worker = New-SiwxCmdWorker $exePath $sid $logPath $resultPath $siwxArguments $isProbe $rootPath
            [IO.File]::WriteAllText($workerPath, $worker, (New-Object Text.UTF8Encoding($false)))
            $launcherPath = Join-Path $bridgeDirectory "$jobId.js"
            $launcher = New-SiwxHiddenLauncher $workerPath $logPath $resultPath
            [IO.File]::WriteAllText($launcherPath, $launcher, [Text.Encoding]::Unicode)
            $currentTask = 'SiwxConsole-' + $jobId
            $description = 'SIWX user-session CMD relay; no automatic triggers'
            if ($backgroundMode) {
                $currentTask = 'SiwxBackground-' + $jobId
                # Only task ownership and diagnostic path metadata belong here.
                $description = @{
                    Kind = 'SiwxConsoleBackground'; UserSid = $sid
                    LogPath = $logPath
                } | ConvertTo-Json -Compress
            }
            $action = New-ScheduledTaskAction -Execute $scriptHost `
                -Argument ('//B //Nologo //E:JScript "' + $launcherPath + '"') `
                -WorkingDirectory $bridgeDirectory
            $principal = New-ScheduledTaskPrincipal -UserId $sid -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $currentTask -Action $action -Principal $principal `
                -Settings $settings -Description $description | Out-Null
            Write-Host "`nStarting as $userId. Output below is from that user."
            Write-Host "Diagnostic task: $currentTask"
            if ($isProbe) { Write-Host 'Checking user-session CMD backend before running siwx.exe...' }
            Write-Host 'Press Q to stop and return to menu; Ctrl+C exits. Web URL: http://127.0.0.1:8787/'
            $interactiveKeyboard = -not [Console]::IsInputRedirected
            Start-ScheduledTask -TaskName $currentTask
            $position = [long]0
            $started = Get-Date
            $stopRequested = $false
            $detached = $false
            $savedUrl = $null
            $stoppedAt = $null
            while ($true) {
                Show-SiwxOutput $logPath ([ref]$position)
                if (Test-Path -LiteralPath $resultPath) { break }
                if ($isServe -and -not $savedUrl -and (Test-SiwxHttp)) {
                    $savedUrl = 'http://127.0.0.1:8787/'
                    Write-Host "Web HTTP check passed: $savedUrl"
                }
                if ($savedUrl -and $backgroundMode -and
                    (Get-ScheduledTask -TaskName $currentTask).State -eq 'Running') {
                    Write-Host 'Background Web is ready. Choose 0 to exit; use 8/9 on a later run to inspect/stop.'
                    $detached = $true
                    break
                }
                if ($isServe -and -not $savedUrl -and ((Get-Date) - $started).TotalSeconds -gt 90) {
                    throw 'Web did not become HTTP-ready within 90 seconds; stopping this task.'
                }
                if ($interactiveKeyboard -and [Console]::KeyAvailable) {
                    $key = [Console]::ReadKey($true)
                    if ($key.Key -eq [ConsoleKey]::Q) { $stopRequested = $true; break }
                }
                $state = (Get-ScheduledTask -TaskName $currentTask).State.ToString()
                if ($state -notin @('Running', 'Queued')) {
                    if (-not $stoppedAt) { $stoppedAt = Get-Date }
                    if (((Get-Date) - $stoppedAt).TotalSeconds -ge 3) {
                        $info = Get-ScheduledTaskInfo -TaskName $currentTask
                        Show-SiwxTaskEvents $currentTask $started
                        $reason = 'Check Task Scheduler and the diagnostic log.'
                        if ([long]$info.LastTaskResult -eq 3221225786) {
                            $reason = '0xC000013A: worker received a console-interruption exit; the interruption source is not established.'
                        }
                        throw "Worker stopped before reporting a result. Task=$currentTask Result=$($info.LastTaskResult). $reason"
                    }
                } else { $stoppedAt = $null }
                if (-not (Test-Path -LiteralPath $logPath) -and ((Get-Date) - $started).TotalSeconds -gt 30) {
                    Show-SiwxTaskEvents $currentTask $started
                    $info = Get-ScheduledTaskInfo -TaskName $currentTask
                    throw "No worker output after 30 seconds. Task=$currentTask State=$state Result=$($info.LastTaskResult)."
                }
                Start-Sleep -Milliseconds 300
            }
            if ($detached) {
                # Explicit background ownership transfer: finally must not stop this task.
                $currentTask = $null
                continue
            }
            if ($stopRequested) {
                Stop-ScheduledTask -TaskName $currentTask
                Write-Host "`nStopped by request."
            } else {
                Show-SiwxOutput $logPath ([ref]$position) $true
                $result = Get-Content -LiteralPath $resultPath -Raw
                Write-Host "`nCommand exit code: $($result.Trim())"
                if ($isProbe) {
                    if ($result.Trim() -ne '0') { throw 'User-session CMD probe failed before siwx.exe was invoked.' }
                    $nextCommand = '1'
                } elseif ($result.Trim() -ne '0') {
                    Show-SiwxTaskEvents $currentTask $started
                }
            }
            Unregister-ScheduledTask -TaskName $currentTask -Confirm:$false
            $currentTask = $null
        }
    } catch {
        Write-Host ("ERROR: " + $_.Exception.Message) -ForegroundColor Red
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
        if ($currentTask) {
            Stop-ScheduledTask -TaskName $currentTask -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $currentTask -Confirm:$false -ErrorAction SilentlyContinue
        }
        if ($bridgeDirectory) {
            Write-Host "Private diagnostic directory: $bridgeDirectory"
            Write-Host 'Logs can contain private account paths. Keep them private.'
        }
    }
}
