#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataDirectory,
    [ValidateSet('0.0.0.0', '127.0.0.1')][string]$ListenAddress = '0.0.0.0',
    [ValidateRange(0, 65535)][int]$Port = 8787,
    [string]$UserId
)

# Task341 / 2026-11-02: SIWX-style menu, owned-port reuse, Limited user-session relay.
# ASCII source supports Windows PowerShell 5.1 when downloaded through irm/iex.
# Pins the exact task338 package and its internal hash manifest. No AV/policy changes.
function ConvertFrom-LiteText([string]$Text) {
    [regex]::Replace($Text, '\\u([0-9a-fA-F]{4})', {
        param($match)
        [string][char][Convert]::ToInt32($match.Groups[1].Value, 16)
    })
}

function Assert-LitePlainPath([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    while ($null -ne $item) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'Reparse points are not allowed in the installation path.'
        }
        if ($item -is [IO.FileInfo]) { $item = $item.Directory }
        else { $item = $item.Parent }
    }
}

function Assert-LiteDistribution([string]$Directory, [string]$ManifestHash) {
    Assert-LitePlainPath $Directory
    $manifest = Join-Path $Directory 'SHA256SUMS.txt'
    Assert-LitePlainPath $manifest
    if ((Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash -ne $ManifestHash) {
        throw 'Manifest SHA-256 mismatch. Existing files were preserved.'
    }
    $expected = @{}
    foreach ($line in Get-Content -LiteralPath $manifest -Encoding UTF8) {
        if ($line -notmatch '^([0-9A-Fa-f]{64})  (.+)$') { throw 'Invalid manifest line.' }
        $hash = $Matches[1]
        $relative = $Matches[2].Replace('/', '\')
        if ([IO.Path]::IsPathRooted($relative) -or $relative -match '^[\\/]' -or $relative.Contains(':') -or
            ($relative.Split('\') | Where-Object { $_ -in @('', '.', '..') })) {
            throw 'Unsafe manifest path.'
        }
        if ($expected.ContainsKey($relative)) { throw 'Duplicate manifest path.' }
        $expected[$relative] = $true
        $file = Join-Path $Directory $relative
        Assert-LitePlainPath $file
        if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $hash) {
            throw ('File SHA-256 mismatch: ' + $relative)
        }
    }
    $separator = [IO.Path]::DirectorySeparatorChar
    $prefix = (Get-Item -LiteralPath $Directory).FullName.TrimEnd($separator) + $separator
    foreach ($file in Get-ChildItem -LiteralPath $Directory -Recurse -Force -File) {
        Assert-LitePlainPath $file.FullName
        $relative = $file.FullName.Substring($prefix.Length).Replace('/', '\')
        if ($relative -ne 'SHA256SUMS.txt' -and -not $expected.ContainsKey($relative)) {
            throw ('Unexpected file in distribution: ' + $relative)
        }
    }
    if (-not $expected.ContainsKey('siwx-lite.exe')) { throw 'Executable missing from manifest.' }
}

function Get-LitePayload([string]$Url, [string]$Destination, [string]$Hash) {
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 180
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Hash) {
        throw 'ZIP SHA-256 mismatch; package will not be extracted or executed.'
    }
}

function Resolve-LiteDesktop($Records, [string]$RequestedSid) {
    $rows = @($Records | Where-Object {
        $_.Sid -match '^S-1-[0-9-]+$' -and $_.Sid -notin @('S-1-5-18', 'S-1-5-19', 'S-1-5-20') -and $_.Session -gt 0
    })
    if ($RequestedSid) { $rows = @($rows | Where-Object Sid -eq $RequestedSid) }
    $sids = @($rows | Select-Object -ExpandProperty Sid -Unique)
    if ($sids.Count -ne 1) { throw 'No unique logged-on desktop user. Log in first, or specify -UserId DOMAIN\user.' }
    $sessions = @($rows | Select-Object -ExpandProperty Session -Unique)
    if ($sessions.Count -ne 1) { throw 'Target user must have exactly one Explorer session.' }
    return [pscustomobject]@{ Sid = $sids[0]; Session = [int]$sessions[0] }
}

function Get-LiteDesktop([string]$RequestedUser) {
    $sid = $null
    if ($RequestedUser) {
        $sid = (New-Object Security.Principal.NTAccount($RequestedUser)).Translate([Security.Principal.SecurityIdentifier]).Value
    }
    $records = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | ForEach-Object {
        $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid
        if ($owner.ReturnValue -eq 0) { [pscustomobject]@{ Sid = $owner.Sid; Session = [int]$_.SessionId } }
    })
    Resolve-LiteDesktop $records $sid
}

function Assert-LiteControlledDirectory([string]$Path, [string]$ReaderSid, [bool]$Writable) {
    Assert-LitePlainPath $Path
    $acl = Get-Acl -LiteralPath $Path
    $admins = @('S-1-5-18', 'S-1-5-32-544')
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $admins) { throw 'Shared path owner is not SYSTEM/Administrators.' }
    $writeMask = [int]([Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles)
    $adminMask = [int]([Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership)
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -ne 'Allow' -or $rule.IdentityReference.Value -in $admins) { continue }
        if ($rule.IdentityReference.Value -ne $ReaderSid -or
            ([int]$rule.FileSystemRights -band $adminMask) -or
            (([int]$rule.FileSystemRights -band $writeMask) -and -not $Writable)) {
            throw 'Unexpected shared-directory permissions; existing ACL preserved.'
        }
    }
}

function New-LiteControlledDirectory([string]$Path, [string]$ReaderSid, [bool]$Writable = $false) {
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
        Assert-LitePlainPath $Path
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $ReaderSid)) {
            $rights = 'FullControl'
            if ($sid -eq $ReaderSid) { $rights = 'ReadAndExecute'; if ($Writable) { $rights = 'Modify' } }
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
                (New-Object Security.Principal.SecurityIdentifier($sid)),
                $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
        Set-Acl -LiteralPath $Path -AclObject $acl
    }
    Assert-LiteControlledDirectory $Path $ReaderSid $Writable
}

function New-LiteUserWorker([string]$ExePath, [string]$Sid, [int]$Session,
    [string]$LogPath, [string]$ResultPath, [string]$Bind, [int]$WebPort, [string]$Data) {
    if ($Sid -notmatch '^S-1-[0-9-]+$' -or $Sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20') -or $Session -le 0) { throw 'Invalid desktop identity.' }
    if ($Bind -notin @('0.0.0.0', '127.0.0.1') -or $WebPort -lt 0 -or $WebPort -gt 65535) { throw 'Invalid network arguments.' }
    foreach ($path in @($ExePath, $LogPath, $ResultPath, $Data)) {
        if ($path -match '[%"\r\n]' -or $path.StartsWith('\\')) { throw 'Unsafe CMD path.' }
    }
    $arguments = '--host ' + $Bind + ' --port ' + $WebPort
    if ($Data) { $arguments += ' --data "' + $Data + '"' }
    $template = @'
@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
"%SystemRoot%\System32\whoami.exe" /user /fo csv /nh >"{LOG}.sid" 2>"{LOG}"
if errorlevel 1 goto identity_error
set "ACTUAL_SID="
for /f "usebackq tokens=2 delims=," %%S in ("{LOG}.sid") do set "ACTUAL_SID=%%~S"
if not "%ACTUAL_SID%"=="{SID}" goto identity_error
>>"{LOG}" echo Stage: desktop identity verified; starting SIWX Lite
"{EXE}" {ARGS} <nul >>"{LOG}" 2>&1
set "LITE_EXIT=%errorlevel%"
goto report
:identity_error
>>"{LOG}" echo ERROR: unexpected desktop SID; EXE was not started
set "LITE_EXIT=5"
:report
>"{RESULT}.tmp" echo %LITE_EXIT%
move /y "{RESULT}.tmp" "{RESULT}" >nul
exit /b %LITE_EXIT%
'@
    $values = @{ LOG = $LogPath; SID = $Sid; EXE = $ExePath; ARGS = $arguments; RESULT = $ResultPath }
    $result = [regex]::Replace($template, '\{(LOG|SID|EXE|ARGS|RESULT)\}', { param($match) $values[$match.Groups[1].Value] })
    return $result.Replace("`r`n", "`n").Replace("`n", "`r`n") + "`r`n"
}

function Show-LiteUserLog([string]$Path, [ref]$Position, [bool]$Final = $false) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Assert-LitePlainPath $Path
    $stream = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
    try {
        if ($Position.Value -gt $stream.Length) { $Position.Value = [long]0 }
        $null = $stream.Seek($Position.Value, 'Begin')
        $bytes = New-Object byte[] ([int]($stream.Length - $Position.Value))
        $count = $stream.Read($bytes, 0, $bytes.Length)
        $end = $count
        if (-not $Final) { while ($end -gt 0 -and $bytes[$end - 1] -ne 10) { $end-- } }
        if ($end -gt 0) { Write-Host -NoNewline ([Text.Encoding]::UTF8.GetString($bytes, 0, $end)); $Position.Value += $end }
    } finally { $stream.Dispose() }
}

function Test-LitePort([ValidateRange(1, 65535)][int]$Port = 8787) {
    # Binding detects listeners, reserved ports and sockets not accepting connections.
    $address = [Net.IPAddress]::Any
    if ($ListenAddress -eq '127.0.0.1') { $address = [Net.IPAddress]::Loopback }
    $listener = New-Object Net.Sockets.TcpListener($address, $Port)
    try {
        $listener.Server.ExclusiveAddressUse = $true
        $listener.Start()
        return $false
    } catch { return $true } finally { $listener.Stop() }
}
function Get-LiteListeningPorts {
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -ge 8787 -and $_.LocalPort -le 8887 } |
        Select-Object -ExpandProperty LocalPort -Unique)
}
function Select-LitePort([string]$ExePath, [string]$UserSid, [int]$SessionId,
    [ValidateRange(1, 65535)][int]$PreferredPort = 8787) {
    $ports = @($PreferredPort) + @(8787..8887 | Where-Object { $_ -ne $PreferredPort })
    foreach ($candidate in $ports) {
        if (-not (Test-LitePort $candidate)) { return [int]$candidate }
        if ((Stop-LitePortOwner $ExePath $UserSid $SessionId $candidate) -and
            -not (Test-LitePort $candidate)) { return [int]$candidate }
    }
    throw (ConvertFrom-LiteText '8787-8887\u7aef\u53e3\u5747\u4e0d\u53ef\u7528\uff0c\u672a\u542f\u52a8\u65b0\u670d\u52a1\u3002')
}
function Get-LiteProcessRecord([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcessId) -ErrorAction Stop
    if (-not $process) { return $null }
    $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
    if ($owner.ReturnValue -ne 0) { throw (ConvertFrom-LiteText '\u65e0\u6cd5\u786e\u8ba4\u8fdb\u7a0b\u6240\u5c5e\u7528\u6237\u3002') }
    [pscustomobject]@{
        Id = [int]$process.ProcessId; ParentId = [int]$process.ParentProcessId
        Name = $process.Name; Path = $process.ExecutablePath; Command = $process.CommandLine
        Sid = $owner.Sid; Session = [int]$process.SessionId; Created = $process.CreationDate
    }
}

function Test-LiteProcessRecord($Record, [string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(1, 65535)][int]$Port = 8787) {
    if (-not $Record -or -not $Record.Created -or $Record.Name -ine 'siwx-lite.exe' -or
        $Record.Sid -ne $UserSid -or $Record.Session -ne $SessionId) { return $false }
    $knownPath = [StringComparer]::OrdinalIgnoreCase.Equals($Record.Path, $ExePath)
    if (-not $knownPath) { return $false }
    $pathPattern = [regex]::Escape($Record.Path)
    $pattern = '^(?:"' + $pathPattern + '"|' + $pathPattern + ')\s+--host\s+(?:0\.0\.0\.0|127\.0\.0\.1)\s+--port\s+' + $Port + '(?:\s+--data\s+(?:"[^"\r\n]+"|[^\s"\r\n]+))?\s*$'
    return $Record.Command -imatch $pattern
}

function Open-LiteProcessHandle($Record, [string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(1, 65535)][int]$Port = 8787) {
    $fresh = Get-LiteProcessRecord $Record.Id
    if (-not (Test-LiteProcessRecord $fresh $ExePath $UserSid $SessionId $Port) -or
        $fresh.Created -ne $Record.Created) { throw (ConvertFrom-LiteText '\u8fdb\u7a0b\u8eab\u4efd\u5df2\u53d8\u5316\uff0c\u672a\u7ed3\u675f\u4efb\u4f55\u8fdb\u7a0b\u3002') }
    $handle = [Diagnostics.Process]::GetProcessById($Record.Id)
    try {
        $null = $handle.Handle
        if ([Math]::Abs(($handle.StartTime.ToUniversalTime() - $Record.Created.ToUniversalTime()).TotalSeconds) -ge 1) {
            throw (ConvertFrom-LiteText '\u8fdb\u7a0b\u542f\u52a8\u65f6\u95f4\u53d8\u5316\uff0c\u62d2\u7edd\u64cd\u4f5c\u5df2\u590d\u7528\u7684PID\u3002')
        }
        return $handle
    } catch { $handle.Dispose(); throw }
}

function Stop-LiteProcessHandle($Handle) {
    if (-not $Handle.HasExited) { $Handle.Kill() }
    if (-not $Handle.WaitForExit(5000)) { throw (ConvertFrom-LiteText 'SIWX\u8fdb\u7a0b\u672a\u80fd\u9000\u51fa\u3002') }
}

function Stop-LitePortOwner([string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(1, 65535)][int]$Port = 8787) {
    $handles = @()
    try {
        $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
        if ($connections.Count -eq 0) { return $true }
        $ids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
        $records = @{}
        foreach ($processId in $ids) {
            $record = Get-LiteProcessRecord $processId
            if (-not (Test-LiteProcessRecord $record $ExePath $UserSid $SessionId $Port)) {
                Write-Host ((ConvertFrom-LiteText "$Port\u7aef\u53e3\u5360\u7528PID={0}\uff1a\u65e0\u6cd5\u786e\u8ba4\u662f\u5f53\u524d\u7528\u6237\u7684SIWX\uff0c\u5df2\u4fdd\u7559\u3002") -f $processId)
                return $false
            }
            $records[$record.Id] = $record
            # PyInstaller one-file has an identical parent supervising the listening child.
            $parent = Get-LiteProcessRecord $record.ParentId
            if ($parent -and $parent.Name -ieq 'siwx-lite.exe') {
                if (-not (Test-LiteProcessRecord $parent $ExePath $UserSid $SessionId $Port) -or
                    $parent.Created -gt $record.Created) {
                    throw (ConvertFrom-LiteText '\u65e0\u6cd5\u786e\u8ba4SIWX\u7236\u8fdb\u7a0b\u8eab\u4efd\uff0c\u672a\u7ed3\u675f\u4efb\u4f55\u8fdb\u7a0b\u3002')
                }
                $records[$parent.Id] = $parent
            }
        }
        # Acquire and validate every original process handle before stopping any process.
        foreach ($record in @($records.Values | Sort-Object Created)) {
            $handles += Open-LiteProcessHandle $record $ExePath $UserSid $SessionId $Port
        }
        foreach ($handle in $handles) { Stop-LiteProcessHandle $handle }
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if (-not (Test-LitePort $Port)) {
                Write-Host (ConvertFrom-LiteText "\u5df2\u7ed3\u675f\u786e\u8ba4\u5f52\u5c5e\u7684SIWX\u6b8b\u7559\u670d\u52a1\uff0c$Port \u7aef\u53e3\u5df2\u91ca\u653e\u3002")
                return $true
            }
            Start-Sleep -Milliseconds 200
        }
        throw (ConvertFrom-LiteText "$Port \u7aef\u53e3\u4ecd\u88ab\u5360\u7528\u3002")
    } catch {
        Write-Host ((ConvertFrom-LiteText "\u672a\u80fd\u91ca\u653e $Port \u7aef\u53e3\uff1a") + $_.Exception.Message)
        return $false
    } finally {
        foreach ($handle in $handles) { $handle.Dispose() }
    }
}


function Get-LiteBackgroundTasks([string]$Root, [string]$Sid, [string]$ExePath) {
    foreach ($task in @(Get-ScheduledTask -TaskName 'SiwxLite-*' -ErrorAction SilentlyContinue)) {
        try {
            if ($task.TaskName -notmatch '^SiwxLite-([a-f0-9]{32})$' -or $task.TaskPath -ne '\') { continue }
            $id = $Matches[1]
            $meta = $task.Description | ConvertFrom-Json
            $worker = Join-Path (Join-Path $Root ('job-' + $id)) 'worker.cmd'
            $log = Join-Path (Join-Path (Join-Path $Root ('job-' + $id)) 'logs') 'output.log'
            if ($meta.Kind -ne 'SiwxLiteMenu341' -or $meta.Sid -ne $Sid -or $task.Principal.UserId -ne $Sid -or
                $meta.Exe -ine $ExePath -or $meta.Log -ine $log -or
                [int]$meta.Port -lt 1 -or [int]$meta.Port -gt 65535 -or
                $meta.Bind -notin @('0.0.0.0', '127.0.0.1') -or [int]$meta.Session -le 0 -or
                @($task.Actions).Count -ne 1 -or
                $task.Actions[0].Execute -ine "$env:SystemRoot\System32\cmd.exe" -or
                $task.Actions[0].Arguments -ne ('/d /s /c ""' + $worker + '""') -or
                $task.Actions[0].WorkingDirectory -ine $Root) { continue }
            Assert-LitePlainPath $worker
            [pscustomobject]@{ Task = $task; Port = [int]$meta.Port; Log = $log; Session = [int]$meta.Session; Bind = $meta.Bind }
        } catch { continue }
    }
}

function Test-LiteService([int]$WebPort, [string]$ExePath, $Desktop) {
    try {
        $connections = @(Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction Stop)
        if ($connections.Count -eq 0) { return $false }
        foreach ($id in @($connections | Select-Object -ExpandProperty OwningProcess -Unique)) {
            if (-not (Test-LiteProcessRecord (Get-LiteProcessRecord $id) $ExePath $Desktop.Sid $Desktop.Session $WebPort)) { return $false }
        }
        $response = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:$WebPort/") -TimeoutSec 2
        return $response.StatusCode -eq 200 -and $response.Content -match 'SIWX'
    } catch { return $false }
}

function Start-LiteUserTask([string]$ExePath, $Desktop, [string]$Root, [bool]$Background = $false) {
    # No triggers, no password, no elevated execution: use the existing interactive token.
    Import-Module ScheduledTasks
    $id = [Guid]::NewGuid().ToString('N')
    $job = Join-Path $Root ('job-' + $id)
    New-LiteControlledDirectory $job $Desktop.Sid $false
    $logs = Join-Path $job 'logs'
    New-LiteControlledDirectory $logs $Desktop.Sid $true
    $worker = Join-Path $job 'worker.cmd'
    $log = Join-Path $logs 'output.log'; $result = Join-Path $logs 'exit.txt'
    $text = New-LiteUserWorker $ExePath $Desktop.Sid $Desktop.Session $log $result $ListenAddress $Port $DataDirectory
    [IO.File]::WriteAllText($worker, $text, (New-Object Text.UTF8Encoding($false)))
    $taskName = 'SiwxLite-' + $id
    $registered = $false; $detached = $false; $cancelled = $false
    try {
        $fresh = Get-LiteDesktop $UserId
        if ($fresh.Sid -ne $Desktop.Sid -or $fresh.Session -ne $Desktop.Session) { throw 'Desktop session changed; refusing launch.' }
        $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument ('/d /s /c ""' + $worker + '""') -WorkingDirectory $Root
        $principal = New-ScheduledTaskPrincipal -UserId $Desktop.Sid -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        $description = @{ Kind='SiwxLiteMenu341'; Sid=$Desktop.Sid; Session=$Desktop.Session; Exe=$ExePath; Port=$Port; Bind=$ListenAddress; Log=$log } | ConvertTo-Json -Compress
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description $description | Out-Null
        $registered = $true
        Write-Host (ConvertFrom-LiteText '\u5df2\u5207\u6362\u5230\u767b\u5f55\u684c\u9762\u7528\u6237\u8fd0\u884c\uff0c\u4ee5\u4e0b\u8f93\u51fa\u56de\u4f20\u5230\u5f53\u524dAgent\u7a97\u53e3\u3002')
        Write-Host ('Task: ' + $taskName + '; Session: ' + $Desktop.Session + '; Log: ' + $log)
        Start-ScheduledTask -TaskName $taskName
        $started = Get-Date; $stopped = $null; $position = [long]0
        while (-not (Test-Path -LiteralPath $result)) {
            Show-LiteUserLog $log ([ref]$position)
            if ($Background -and (Test-LiteService $Port $ExePath $Desktop)) {
                Write-Host (ConvertFrom-LiteText "\u540e\u53f0\u7f51\u9875\u5df2\u5c31\u7eea\uff1ahttp://127.0.0.1:$Port/\uff1b8\u67e5\u770b\u72b6\u6001\uff0c9\u505c\u6b62\u3002")
                $detached = $true
                return
            }
            if (-not [Console]::IsInputRedirected -and [Console]::KeyAvailable -and [Console]::ReadKey($true).Key -eq [ConsoleKey]::Q) {
                $cancelled = $true
                break
            }
            $state = (Get-ScheduledTask -TaskName $taskName).State.ToString()
            if ($state -notin @('Running', 'Queued')) {
                if (-not $stopped) { $stopped = Get-Date }
                if (((Get-Date) - $stopped).TotalSeconds -ge 3) {
                    $info = Get-ScheduledTaskInfo -TaskName $taskName
                    throw ('Desktop task stopped without result; code=' + $info.LastTaskResult + '; log=' + $log)
                }
            } else { $stopped = $null }
            if (-not (Test-Path -LiteralPath $log) -and ((Get-Date) - $started).TotalSeconds -gt 30) { throw ('No desktop output within 30 seconds; log=' + $log) }
            if ($Background -and ((Get-Date) - $started).TotalSeconds -gt 90) { throw ('Web not ready within 90 seconds; log=' + $log) }
            Start-Sleep -Milliseconds 500
        }
        if ($cancelled) { return }
        Show-LiteUserLog $log ([ref]$position) $true
        Assert-LitePlainPath $result
        if ((Get-Item -LiteralPath $result).Length -gt 32) { throw 'Invalid desktop exit marker size.' }
        $code = (Get-Content -LiteralPath $result -Raw).Trim()
        if ($code -notmatch '^\d{1,10}$') { throw 'Invalid desktop exit marker.' }
        if ($code -ne '0') { throw ('SIWX Lite desktop worker exited: ' + $code + '; log=' + $log) }
    } finally {
        if ($registered -and -not $detached) {
            $own = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($own -and $own.Principal.UserId -eq $Desktop.Sid -and $own.Actions[0].Arguments -eq ('/d /s /c ""' + $worker + '""')) {
                if ($own.State.ToString() -in @('Running', 'Queued')) { Stop-ScheduledTask -TaskName $taskName -ErrorAction Continue }
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Continue
            }
            if (Test-LitePort $Port) { $null = Stop-LitePortOwner $ExePath $Desktop.Sid $Desktop.Session $Port }
        }
        # Keep this private diagnostic job for troubleshooting; no unrelated cleanup.
    }
}


function Show-LiteMenu([string]$ExePath, $Desktop, [string]$Root, [bool]$Service) {
    $preferred = $Port
    if ($preferred -eq 0) { $preferred = 8787 }
    while ($true) {
        Write-Host (ConvertFrom-LiteText "`nSIWX \u7cbe\u7b80\u7248\u4e2d\u6587\u83dc\u5355 v1.3\uff08\u7a0b\u5e8f1.2.0\uff09  \u9996\u9009\u7aef\u53e3=$preferred  \u76d1\u542c=$ListenAddress")
        Write-Host (ConvertFrom-LiteText '1 = \u73af\u5883\u68c0\u67e5   2 = \u542f\u52a8\u7f51\u9875\u8fdb\u884c\u81ea\u52a8\u63d0\u53d6 / \u89e3\u5bc6 / \u5bfc\u51fa')
        Write-Host (ConvertFrom-LiteText '6 = \u542f\u52a8\u7f51\u9875\u5e76\u663e\u793a\u65e5\u5fd7\uff08Q\u8fd4\u56de\u83dc\u5355\uff09   7 = \u540e\u53f0\u542f\u52a8\u7f51\u9875')
        Write-Host (ConvertFrom-LiteText '8 = \u540e\u53f0\u72b6\u6001 / \u7f51\u5740 / \u65e5\u5fd7   9 = \u505c\u6b62\u672c\u5de5\u5177\u7f51\u9875   0 = \u9000\u51fa\u83dc\u5355')
        Write-Host (ConvertFrom-LiteText '12 = \u5168\u76d8\u641c\u7d22 xwechat_files   13 = \u4ec5\u641c\u7d22C\u76d8   14 = \u6307\u5b9a / \u6e05\u9664\u6570\u636e\u76ee\u5f55')
        Write-Host (ConvertFrom-LiteText '15 = \u8bbe\u7f6e\u9996\u9009\u7aef\u53e3   16 = \u66f4\u6362\u76ee\u6807\u684c\u9762\u7528\u6237\uff08SYSTEM\u5165\u53e3\uff09')
        Write-Host (ConvertFrom-LiteText '\u63d0\u53d6\u3001\u804a\u5929\u6d4f\u89c8\u548c\u5bfc\u51fa\u5728\u7f51\u9875\u5b8c\u6210\uff1b\u4e0d\u542b\u7edf\u8ba1\u3001\u52a0\u5bc6\u5907\u4efd\u6216MCP\u3002\u7f51\u9875\u65e0\u5bc6\u7801\uff0c\u52ff\u8f6c\u53d1\u5230\u4e0d\u53d7\u4fe1\u4efb\u7f51\u7edc\u3002')
        $choice = Read-Host (ConvertFrom-LiteText '\u8bf7\u9009\u62e9')
        if ($choice -eq '0') { break }
        try {
            if ($choice -eq '1') {
                Write-Host ('EXE: ' + $ExePath)
                Write-Host ('User SID: ' + $Desktop.Sid + '; Session: ' + $Desktop.Session)
                Write-Host ('Data: ' + $DataDirectory)
                Assert-LiteDistribution (Split-Path -Parent $ExePath) 'a1279e49ee78c343cac7c9d28824c41a9bcca992d1d9d2b180032a458b35d438'
                Write-Host (ConvertFrom-LiteText '\u7a0b\u5e8f\u5b8c\u6574\u6027\u901a\u8fc7\uff1b\u5f53\u524d\u9996\u9009\u7aef\u53e3\u5360\u7528\uff1a') (Test-LitePort $preferred)
                continue
            }
            if ($choice -in @('12','13')) {
                $roots = @('C:')
                if ($choice -eq '12') { $roots = @(Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Root -Unique) }
                Write-Host (ConvertFrom-LiteText '\u53ea\u641c\u7d22\u6587\u4ef6\u5939\u540d\u79f0\uff0c\u4e0d\u8bfb\u53d6\u804a\u5929\uff1b\u5927\u78c1\u76d8\u53ef\u80fd\u8f83\u6162\uff0c\u65e0\u6743\u9650\u76ee\u5f55\u8df3\u8fc7\u3002')
                foreach ($searchRoot in $roots) {
                    Get-ChildItem -LiteralPath $searchRoot -Directory -Filter xwechat_files -Recurse -Force -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName | ForEach-Object { Write-Host $_ }
                }
                continue
            }
            if ($choice -eq '14') {
                $answer = Read-Host (ConvertFrom-LiteText '\u8f93\u5165\u5305\u542bdb_storage\u7684\u8d26\u53f7\u76ee\u5f55\uff1b\u76f4\u63a5\u56de\u8f66\u6062\u590d\u81ea\u52a8\u53d1\u73b0')
                if ($answer) {
                    $data = Get-Item -LiteralPath $answer -ErrorAction Stop
                    if (-not $data.PSIsContainer -or -not (Test-Path -LiteralPath (Join-Path $data.FullName 'db_storage') -PathType Container)) { throw 'Directory must contain db_storage.' }
                    if ($data.FullName -match '[%"
]' -or $data.FullName.StartsWith('\\')) { throw 'Unsafe CMD data directory.' }
                    $DataDirectory = $data.FullName
                } else { $DataDirectory = '' }
                Write-Host (ConvertFrom-LiteText '\u76ee\u5f55\u4ec5\u5bf9\u4e0b\u6b21\u542f\u52a8\u751f\u6548\uff0c\u4e0d\u5f71\u54cd\u5df2\u8fd0\u884c\u7f51\u9875\u3002')
                continue
            }
            if ($choice -eq '15') {
                $answer = Read-Host (ConvertFrom-LiteText '\u9996\u9009\u7aef\u53e3\uff081-65535\uff0c\u56de\u8f66\u6062\u590d8787\uff09')
                $parsed = 8787
                if ($answer -and (-not [int]::TryParse($answer, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 65535)) { throw 'Invalid port.' }
                $preferred = $parsed
                continue
            }
            if ($choice -eq '16') {
                if (-not $Service) { throw 'User selection is available only from the SYSTEM entry.' }
                $UserId = Read-Host (ConvertFrom-LiteText '\u76ee\u6807Windows\u7528\u6237\uff08\u57df\u540d\\u7528\u6237\u540d\uff09\uff0c\u56de\u8f66\u81ea\u52a8\u8bc6\u522b\u552f\u4e00\u684c\u9762')
                $Desktop = Get-LiteDesktop $UserId
                continue
            }
            if ($choice -in @('8','9')) {
                $tasks = @()
                if ($Service) { $tasks = @(Get-LiteBackgroundTasks $Root $Desktop.Sid $ExePath) }
                for ($i=0; $i -lt $tasks.Count; $i++) {
                    $item = $tasks[$i]
                    Write-Host ("[{0}] {1}  {2}  http://127.0.0.1:{3}/" -f ($i+1),$item.Task.TaskName,$item.Task.State,$item.Port)
                    Write-Host ('Log: ' + $item.Log)
                    if ($choice -eq '8' -and (Test-Path -LiteralPath $item.Log)) {
                        Assert-LitePlainPath $item.Log
                        Get-Content -LiteralPath $item.Log -Tail 20 -Encoding UTF8 | ForEach-Object { Write-Host $_ }
                    }
                }
                if ($tasks.Count -eq 0) { Write-Host (ConvertFrom-LiteText '\u6ca1\u6709\u672c\u811a\u672c\u767b\u8bb0\u7684\u540e\u53f0\u4efb\u52a1\uff1b9\u4ecd\u53ef\u6838\u67e5\u5e76\u505c\u6b62\u672c\u7528\u6237\u3001\u672cEXE\u7684\u7aef\u53e3\u6b8b\u7559\u3002') }
                if ($choice -eq '9') {
                    if ($tasks.Count -gt 0) {
                        $answer = Read-Host (ConvertFrom-LiteText '\u8f93\u5165\u8981\u505c\u6b62\u7684\u4efb\u52a1\u7f16\u53f7\uff0c\u56de\u8f66\u53d6\u6d88')
                        $number = 0
                        if ([int]::TryParse($answer,[ref]$number) -and $number -ge 1 -and $number -le $tasks.Count) {
                            $selected = $tasks[$number-1]
                            # Re-read exact identity immediately before changing scheduler state.
                            $fresh = @(Get-LiteBackgroundTasks $Root $Desktop.Sid $ExePath | Where-Object { $_.Task.TaskName -eq $selected.Task.TaskName })
                            if ($fresh.Count -ne 1) { throw 'Task identity changed.' }
                            $null = Stop-LitePortOwner $ExePath $Desktop.Sid $selected.Session $selected.Port
                            Stop-ScheduledTask -TaskName $selected.Task.TaskName -ErrorAction Stop
                            Unregister-ScheduledTask -TaskName $selected.Task.TaskName -Confirm:$false -ErrorAction Stop
                        }
                    } else {
                        $ports = @($preferred) + @(Get-LiteListeningPorts | Where-Object { $_ -ne $preferred })
                        foreach ($candidate in $ports) { if (Test-LitePort $candidate) { $null = Stop-LitePortOwner $ExePath $Desktop.Sid $Desktop.Session $candidate } }
                    }
                }
                continue
            }
            if ($choice -notin @('2','6','7','10','11')) { continue }
            $background = $choice -in @('7','11')
            if ($background -and -not $Service) { throw 'Background mode requires the Agent SYSTEM entry; use menu6 in a normal desktop shell.' }
            if ($Service) {
                $active = @(Get-LiteBackgroundTasks $Root $Desktop.Sid $ExePath | Where-Object { $_.Task.State.ToString() -in @('Running','Queued') })
                if ($active.Count -gt 0) { Write-Host (ConvertFrom-LiteText '\u5df2\u6709\u540e\u53f0\u4efb\u52a1\uff0c\u8bf7\u5148\u75288\u67e5\u770b\u62169\u505c\u6b62\uff0c\u907f\u514d\u91cd\u590d\u542f\u52a8\u3002'); continue }
            }
            Assert-LiteDistribution (Split-Path -Parent $ExePath) 'a1279e49ee78c343cac7c9d28824c41a9bcca992d1d9d2b180032a458b35d438'
            $Port = Select-LitePort $ExePath $Desktop.Sid $Desktop.Session $preferred
            Write-Host (ConvertFrom-LiteText "\u5b9e\u9645\u7aef\u53e3=$Port\uff1b\u672c\u673a\u7f51\u5740=http://127.0.0.1:$Port/\uff1b\u82e5\u6709\u53cd\u4ee3\u6216\u5185\u7f51\u7a7f\u900f\uff0c\u8bf7\u540c\u6b65\u76ee\u6807\u7aef\u53e3\u3002")
            Write-Host (ConvertFrom-LiteText '\u672a\u786e\u8ba4\u5f52\u5c5e\u7684\u5360\u7528\u4e0d\u4f1a\u7ed3\u675f\uff1b\u7f51\u9875\u7ed1\u5b9a0.0.0.0\u65f6\u53ef\u7528\u672c\u673a\u7f51\u7edcIP\u8bbf\u95ee\u3002')
            if ($Service) { Start-LiteUserTask $ExePath $Desktop $Root $background }
            else {
                $arguments = @('--host',$ListenAddress,'--port',[string]$Port)
                if ($DataDirectory) { $arguments += @('--data',$DataDirectory) }
                Write-Host (ConvertFrom-LiteText '\u8bf7\u7528\u7f51\u9875\u505c\u6b62\u8fd4\u56de\u83dc\u5355\uff1bCtrl+C\u4e5f\u53ef\u9000\u51fa\u3002')
                & $ExePath @arguments
                if ($LASTEXITCODE -ne 0) { Write-Host ('SIWX Lite exit=' + $LASTEXITCODE) }
            }
        } catch { Write-Host ((ConvertFrom-LiteText '\u64cd\u4f5c\u672a\u5b8c\u6210\uff1a') + $_.Exception.Message) -ForegroundColor Red }
    }
}

function Start-LiteMain {
    $ErrorActionPreference = 'Stop'
    if ($env:OS -ne 'Windows_NT') { throw 'Run in Windows PowerShell on your own desktop.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $service = $identity.User.Value -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')
        if ($service -and -not $identity.IsSystem) { throw 'Service relay requires SYSTEM; local-service/network-service are not elevated.' }
    } finally { $identity.Dispose() }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
    $desktop = $null
    if ($service) {
        try { $desktop = Get-LiteDesktop $UserId }
        catch {
            Write-Host $_.Exception.Message
            $UserId = Read-Host (ConvertFrom-LiteText '\u8bf7\u8f93\u5165\u5df2\u767b\u5f55\u684c\u9762\u7528\u6237\uff08\u57df\u540d\\u7528\u6237\u540d\uff09')
            if (-not $UserId) { throw 'No target desktop selected.' }
            $desktop = Get-LiteDesktop $UserId
        }
    }
    else {
        $self = [Security.Principal.WindowsIdentity]::GetCurrent()
        try { $desktop = [pscustomobject]@{Sid=$self.User.Value;Session=[Diagnostics.Process]::GetCurrentProcess().SessionId} }
        finally { $self.Dispose() }
    }
    if (-not $service -and [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }

    $zipHash = '6e892c238e0c19d51682e605a349f0903189637a993a2aeb69d4a9a50f16dc23'
    $manifestHash = 'a1279e49ee78c343cac7c9d28824c41a9bcca992d1d9d2b180032a458b35d438'
    $url = 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite-1.2.0-492f397-windows-x64.zip'
    if ($service) {
        $root = Join-Path $env:ProgramData 'SIWX-Lite-Session'
        New-LiteControlledDirectory $root 'S-1-5-11' $false
    } else { $root = Join-Path $env:LOCALAPPDATA 'SIWX-Lite' }
    $version = Join-Path $root '1.2.0-492f397'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    Assert-LitePlainPath $root
    if (-not (Test-Path -LiteralPath $version)) {
        $stage = Join-Path $root ('.stage-' + [Guid]::NewGuid().ToString('N'))
        $zip = $stage + '.zip'
        try {
            Write-Host (ConvertFrom-LiteText '\u6b63\u5728\u4e0b\u8f7dSIWX\u7cbe\u7b80\u72481.2.0\uff0c\u4e0b\u8f7d\u540e\u6821\u9a8c\u5168\u90e8\u6587\u4ef6...')
            Get-LitePayload $url $zip $zipHash
            Expand-Archive -LiteralPath $zip -DestinationPath $stage -ErrorAction Stop
            Assert-LiteDistribution $stage $manifestHash
            if (Test-Path -LiteralPath $version) { throw 'Version directory appeared during installation; retry.' }
            Move-Item -LiteralPath $stage -Destination $version
        } finally {
            if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
            if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
        }
    }
    Assert-LiteDistribution $version $manifestHash
    if ($service) {
        Assert-LiteControlledDirectory $version 'S-1-5-11' $false
        foreach ($file in Get-ChildItem -LiteralPath $version -Recurse -Force -File) {
            Assert-LiteControlledDirectory $file.FullName 'S-1-5-11' $false
        }
    }
    $exe = Join-Path $version 'siwx-lite.exe'
    Show-LiteMenu $exe $desktop $root $service
}

Start-LiteMain
