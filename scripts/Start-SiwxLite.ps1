#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataDirectory,
    [ValidateSet('0.0.0.0', '127.0.0.1')][string]$ListenAddress = '0.0.0.0',
    [ValidateRange(0, 65535)][int]$Port = 8787,
    [string]$UserId
)

# Task340 / 2026-11-02: desktop direct launch or one-time Limited user-session relay.
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

function Start-LiteUserTask([string]$ExePath, $Desktop, [string]$Root) {
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
    $registered = $false
    try {
        $fresh = Get-LiteDesktop $UserId
        if ($fresh.Sid -ne $Desktop.Sid -or $fresh.Session -ne $Desktop.Session) { throw 'Desktop session changed; refusing launch.' }
        $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument ('/d /s /c ""' + $worker + '""') -WorkingDirectory $Root
        $principal = New-ScheduledTaskPrincipal -UserId $Desktop.Sid -LogonType Interactive -RunLevel Limited
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings -Description 'SIWX Lite one-time desktop relay; no automatic triggers' | Out-Null
        $registered = $true
        Write-Host (ConvertFrom-LiteText '\u5df2\u5207\u6362\u5230\u767b\u5f55\u684c\u9762\u7528\u6237\u8fd0\u884c\uff0c\u4ee5\u4e0b\u8f93\u51fa\u56de\u4f20\u5230\u5f53\u524dAgent\u7a97\u53e3\u3002')
        Write-Host ('Task: ' + $taskName + '; Session: ' + $Desktop.Session + '; Log: ' + $log)
        Start-ScheduledTask -TaskName $taskName
        $started = Get-Date; $stopped = $null; $position = [long]0
        while (-not (Test-Path -LiteralPath $result)) {
            Show-LiteUserLog $log ([ref]$position)
            $state = (Get-ScheduledTask -TaskName $taskName).State.ToString()
            if ($state -notin @('Running', 'Queued')) {
                if (-not $stopped) { $stopped = Get-Date }
                if (((Get-Date) - $stopped).TotalSeconds -ge 3) {
                    $info = Get-ScheduledTaskInfo -TaskName $taskName
                    throw ('Desktop task stopped without result; code=' + $info.LastTaskResult + '; log=' + $log)
                }
            } else { $stopped = $null }
            if (-not (Test-Path -LiteralPath $log) -and ((Get-Date) - $started).TotalSeconds -gt 30) { throw ('No desktop output within 30 seconds; log=' + $log) }
            Start-Sleep -Milliseconds 500
        }
        Show-LiteUserLog $log ([ref]$position) $true
        Assert-LitePlainPath $result
        if ((Get-Item -LiteralPath $result).Length -gt 32) { throw 'Invalid desktop exit marker size.' }
        $code = (Get-Content -LiteralPath $result -Raw).Trim()
        if ($code -notmatch '^\d{1,10}$') { throw 'Invalid desktop exit marker.' }
        if ($code -ne '0') { throw ('SIWX Lite desktop worker exited: ' + $code + '; log=' + $log) }
    } finally {
        if ($registered) {
            $own = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            if ($own -and $own.Principal.UserId -eq $Desktop.Sid -and $own.Actions[0].Arguments -eq ('/d /s /c ""' + $worker + '""')) {
                if ($own.State.ToString() -in @('Running', 'Queued')) { Stop-ScheduledTask -TaskName $taskName -ErrorAction Continue }
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Continue
            }
        }
        # Keep this private diagnostic job for troubleshooting; no unrelated cleanup.
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
    if ($service) { $desktop = Get-LiteDesktop $UserId }
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
    $arguments = @('--host', $ListenAddress, '--port', [string]$Port)
    if (-not [string]::IsNullOrWhiteSpace($DataDirectory)) {
        $data = Get-Item -LiteralPath $DataDirectory -ErrorAction Stop
        if (-not $data.PSIsContainer -or -not (Test-Path -LiteralPath (Join-Path $data.FullName 'db_storage') -PathType Container)) {
            throw 'DataDirectory must contain db_storage.'
        }
        $arguments += @('--data', $data.FullName)
    }
    if ($service) { Start-LiteUserTask $exe $desktop $root; return }
    Write-Host (ConvertFrom-LiteText '\u6821\u9a8c\u901a\u8fc7\uff0c\u524d\u53f0\u542f\u52a8\u3002\u65e0\u4ee4\u724c\u65e0\u5bc6\u7801\uff0c\u80fd\u8fde\u63a5\u7aef\u53e3\u7684\u8bbe\u5907\u5747\u53ef\u64cd\u4f5c\u3002')
    Write-Host (ConvertFrom-LiteText '\u4fdd\u7559\u672c\u7a97\u53e3\uff0c\u624b\u52a8\u6253\u5f00\u7a0b\u5e8f\u8f93\u51fa\u7684\u7f51\u5740\uff1b\u7f51\u9875\u505c\u6b62\u6216Ctrl+C\u9000\u51fa\u3002')
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ('SIWX Lite exited with code ' + $LASTEXITCODE) }
}

Start-LiteMain
