#Requires -Version 5.1
# One-command Windows entry; no Python installation required.
# Task356: persistent export history; trusted previous-release port cleanup, loopback only.
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
        Write-Host (ConvertFrom-ConsoleText 'SIWX \u4e2d\u6587\u63a7\u5236\u53f0 v1.17\uff1a\u4ee5\u9009\u5b9a\u7684\u5df2\u767b\u5f55\u684c\u9762\u7528\u6237\u8fd0\u884c\uff0c\u4e0d\u81ea\u52a8\u6253\u5f00\u6d4f\u89c8\u5668\u3002')
        $suggestedUser = (Get-CimInstance Win32_ComputerSystem).UserName
        $userId = Read-Host (ConvertFrom-ConsoleText "\u76ee\u6807 Windows \u7528\u6237\uff08\u57df\u540d\\u7528\u6237\u540d\uff09\uff0c\u56de\u8f66\u4f7f\u7528 [$suggestedUser]")
        if (-not $userId) { $userId = $suggestedUser }
        if (-not $userId) { throw (ConvertFrom-ConsoleText '\u8bf7\u5148\u767b\u5f55 Windows \u684c\u9762\u3002') }
        $sid = (New-Object Security.Principal.NTAccount($userId)).Translate(
            [Security.Principal.SecurityIdentifier]).Value
        if ($sid -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) { throw (ConvertFrom-ConsoleText '\u8bf7\u9009\u62e9\u684c\u9762\u7528\u6237\uff0c\u4e0d\u80fd\u4f7f\u7528\u7cfb\u7edf\u670d\u52a1\u8d26\u53f7\u3002') }
        $userId = (New-Object Security.Principal.SecurityIdentifier($sid)).Translate([Security.Principal.NTAccount]).Value
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
        function Assert-SiwxInstallAcl([string]$Path) {
            Assert-SiwxLocalPath $Path
            $acl = Get-Acl -LiteralPath $Path
            $writers = @('S-1-5-18', 'S-1-5-32-544')
            $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            if ($ownerSid -notin $writers) { throw (ConvertFrom-ConsoleText '\u7a0b\u5e8f\u8def\u5f84\u6240\u6709\u8005\u5fc5\u987b\u662f\u7ba1\u7406\u5458\u6216SYSTEM\u3002') }
            $writeMask = [Security.AccessControl.FileSystemRights]::Write -bor
                [Security.AccessControl.FileSystemRights]::Delete -bor
                [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
                [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
                [Security.AccessControl.FileSystemRights]::TakeOwnership
            foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
                if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $writers -and
                    ([int]$rule.FileSystemRights -band $writeMask)) {
                    throw (ConvertFrom-ConsoleText '\u7a0b\u5e8f\u8def\u5f84\u5141\u8bb8\u975e\u7ba1\u7406\u5458\u5199\u5165\uff0c\u5df2\u4fdd\u7559\u539f\u6743\u9650\u5e76\u62d2\u7edd\u6267\u884c\u3002')
                }
            }
        }
        function New-SiwxInstallDirectory([string]$Path) {
            Assert-SiwxLocalPath $Path
            if (-not (Test-Path -LiteralPath $Path)) {
                New-Item -ItemType Directory -Path $Path | Out-Null
                $acl = New-Object Security.AccessControl.DirectorySecurity
                $acl.SetAccessRuleProtection($true, $false)
                $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')))
                foreach ($accessSid in @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-11')) {
                    $rights = 'ReadAndExecute'
                    if ($accessSid -ne 'S-1-5-11') { $rights = 'FullControl' }
                    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                        (New-Object Security.Principal.SecurityIdentifier($accessSid)),
                        $rights, 'ContainerInherit,ObjectInherit', 'None', 'Allow')
                    $acl.AddAccessRule($rule)
                }
                Set-Acl -LiteralPath $Path -AclObject $acl
            }
            if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw (ConvertFrom-ConsoleText '\u7a0b\u5e8f\u8def\u5f84\u4e0d\u662f\u6587\u4ef6\u5939\u3002') }
            Assert-SiwxInstallAcl $Path
        }
        $rootPath = Join-Path $profile.LocalPath 'AppData\Local\stories-in-wx-console'
        New-SiwxPrivateDirectory $rootPath $sid
        $legacyExePath = $null
        $installBase = Join-Path $env:SystemRoot 'SIWX'
        New-SiwxInstallDirectory $installBase
        $installPath = Join-Path $installBase 'lite-1.6.1-c7e7a9b'
        $exePath = Join-Path $installPath 'siwx-lite.exe'
        $expectedZipHash = 'e0c8d40e49d2b6078d49569bf731e8e89976db2cc250b82a45f4a8c5e3ab206e'
        $manifestHash = '94026daba377efb0b03e68a791e240f382b4f11487ac2acc4aadbec026e80e10'
        $downloadUrl = 'http://139.196.47.109:9939/AAAAAAAA/siwx-native-1.6.1-c7e7a9b-windows-x64.zip'
        [Net.ServicePointManager]::SecurityProtocol = $previousTls -bor [Net.SecurityProtocolType]::Tls12
        function Assert-SiwxPlainPath([string]$Path) {
            $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
            while ($null -ne $item) {
                if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                    throw 'Reparse points are not allowed in the installation path.'
                }
                if ($item -is [IO.FileInfo]) { $item = $item.Directory }
                else { $item = $item.Parent }
            }
        }

        function Assert-SiwxDistribution([string]$Directory, [string]$ManifestHash) {
            Assert-SiwxPlainPath $Directory
            $manifest = Join-Path $Directory 'SHA256SUMS.txt'
            Assert-SiwxPlainPath $manifest
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
                Assert-SiwxPlainPath $file
                if (-not (Test-Path -LiteralPath $file -PathType Leaf) -or
                    (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $hash) {
                    throw ('File SHA-256 mismatch: ' + $relative)
                }
            }
            $separator = [IO.Path]::DirectorySeparatorChar
            $prefix = (Get-Item -LiteralPath $Directory).FullName.TrimEnd($separator) + $separator
            foreach ($file in Get-ChildItem -LiteralPath $Directory -Recurse -Force -File) {
                Assert-SiwxPlainPath $file.FullName
                $relative = $file.FullName.Substring($prefix.Length).Replace('/', '\')
                if ($relative -ne 'SHA256SUMS.txt' -and -not $expected.ContainsKey($relative)) {
                    throw ('Unexpected file in distribution: ' + $relative)
                }
            }
            if (-not $expected.ContainsKey('siwx-lite.exe')) { throw 'Executable missing from manifest.' }
        }

        function Get-SiwxPayload([string]$Url, [string]$Destination, [string]$Hash) {
            Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec 180
            if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Hash) {
                throw 'ZIP SHA-256 mismatch; package will not be extracted or executed.'
            }
        }

        function Install-SiwxRelease([string]$Directory, [string]$DownloadUrl,
            [string]$ZipHash, [string]$ManifestHash) {
            if (-not (Test-Path -LiteralPath $Directory)) {
                $stage = $Directory + '.' + [Guid]::NewGuid().ToString('N') + '.stage'
                $zip = $stage + '.zip'
                try {
                    New-SiwxInstallDirectory $stage
                    Get-SiwxPayload $DownloadUrl $zip $ZipHash
                    Expand-Archive -LiteralPath $zip -DestinationPath $stage
                    Assert-SiwxDistribution $stage $ManifestHash
                    if (Test-Path -LiteralPath $Directory) { throw 'Version appeared during install; retry.' }
                    Move-Item -LiteralPath $stage -Destination $Directory
                } finally {
                    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
                    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
                }
            }
            Assert-SiwxDistribution $Directory $ManifestHash
            Assert-SiwxInstallAcl $Directory
            foreach ($file in Get-ChildItem -LiteralPath $Directory -Recurse -Force -File) {
                Assert-SiwxInstallAcl $file.FullName
            }
        }
        Install-SiwxRelease $installPath $downloadUrl $expectedZipHash $manifestHash
        $dataDirectory = $env:SIWX_LITE_DATA
        if ($dataDirectory) {
            Assert-SiwxLocalPath $dataDirectory
            if (-not (Test-Path -LiteralPath (Join-Path $dataDirectory 'db_storage') -PathType Container)) {
                throw 'SIWX_LITE_DATA must be an account directory containing db_storage.'
            }
        }
        $preferredPort = 8787
        Write-Host (ConvertFrom-ConsoleText "\u7a0b\u5e8f\u4f4d\u7f6e\uff1a$exePath")
        Write-Host (ConvertFrom-ConsoleText "\u8fd0\u884c\u53ca\u8f93\u51fa\u76ee\u5f55\uff1a$rootPath")
        Write-Host 'Lite 1.6.1 / 127.0.0.1 / no IP, Host, Origin, token or password checks.'
        $bridgeDirectory = Join-Path "$env:SystemRoot\Temp" ('SiwxBridge-' + [Guid]::NewGuid().ToString('N'))
        New-SiwxPrivateDirectory $bridgeDirectory $sid

        function Test-SiwxPort([ValidateRange(8787, 8887)][int]$Port = 8787) {
            # Binding detects listeners, reserved ports and sockets not accepting connections.
            $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $Port)
            try {
                $listener.Server.ExclusiveAddressUse = $true
                $listener.Start()
                return $false
            } catch { return $true } finally { $listener.Stop() }
        }
        function Get-SiwxListeningPorts {
            @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
                Where-Object { $_.LocalPort -ge 8787 -and $_.LocalPort -le 8887 } |
                Select-Object -ExpandProperty LocalPort -Unique)
        }
        function Select-SiwxPort([string]$ExePath, [string]$UserSid, [int]$SessionId,
            [ValidateRange(8787, 8887)][int]$PreferredPort = 8787) {
            $ports = @($PreferredPort) + @(8787..8887 | Where-Object { $_ -ne $PreferredPort })
            foreach ($candidate in $ports) {
                if (-not (Test-SiwxPort $candidate)) { return [int]$candidate }
                if ((Stop-SiwxPortOwner $ExePath $UserSid $SessionId $candidate) -and
                    -not (Test-SiwxPort $candidate)) { return [int]$candidate }
            }
            throw (ConvertFrom-ConsoleText '8787-8887\u7aef\u53e3\u5747\u4e0d\u53ef\u7528\uff0c\u672a\u542f\u52a8\u65b0\u670d\u52a1\u3002')
        }
        function Get-SiwxProcessRecord([int]$ProcessId) {
            $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ProcessId) -ErrorAction Stop
            if (-not $process) { return $null }
            $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -ErrorAction Stop
            if ($owner.ReturnValue -ne 0) { throw (ConvertFrom-ConsoleText '\u65e0\u6cd5\u786e\u8ba4\u8fdb\u7a0b\u6240\u5c5e\u7528\u6237\u3002') }
            [pscustomobject]@{
                Id = [int]$process.ProcessId; ParentId = [int]$process.ParentProcessId
                Name = $process.Name; Path = $process.ExecutablePath; Command = $process.CommandLine
                Sid = $owner.Sid; Session = [int]$process.SessionId; Created = $process.CreationDate
            }
        }

        function Test-SiwxTrustedInstall([string]$CandidatePath, [string]$CurrentExePath) {
            if ([StringComparer]::OrdinalIgnoreCase.Equals($CandidatePath, $CurrentExePath)) { return $true }
            # Only exact previous releases installed by this launcher. Neither a process
            # name, HTTP title nor an arbitrary similarly named folder is sufficient.
            $releases = @{
                'lite-1.5.0-9b2b748' = 'ff43d33b8ac01935994413316f34bc4e31732f4cb60826da64b37d8651cfbd90'
                'lite-1.5.1-153c979' = 'b4d1ab713bfffd00be42e570e8283dacd79eded3fe775c933f79c05ccd3418c5'
                'lite-1.5.2-4f20738' = 'a96f7e62b768c1b544546d9aa7c71139f15cf19b9266f4743ef168b606d61cd2'
                'lite-1.6.0-9419f05' = 'bd9ed72c17903dc344b20b880f28828df8797a21febec5dae470c52d5573a688'
            }
            try {
                $base = Join-Path $env:SystemRoot 'SIWX'
                $directory = [IO.Path]::GetDirectoryName($CandidatePath)
                $version = [IO.Path]::GetFileName($directory)
                if (-not $releases.ContainsKey($version)) { return $false }
                $expected = Join-Path (Join-Path $base $version) 'siwx-lite.exe'
                if (-not [StringComparer]::OrdinalIgnoreCase.Equals($CandidatePath, $expected)) { return $false }
                Assert-SiwxPlainPath $CandidatePath
                Assert-SiwxInstallAcl $base
                Assert-SiwxInstallAcl $directory
                Assert-SiwxDistribution $directory $releases[$version]
                foreach ($file in Get-ChildItem -LiteralPath $directory -Recurse -Force -File) {
                    Assert-SiwxInstallAcl $file.FullName
                }
                return $true
            } catch { return $false }
        }

        function Get-SiwxPortProbe([ValidateRange(8787, 8887)][int]$Port = 8787) {
            # Diagnostics only. A reachable socket/HTTP endpoint never authorizes kill.
            $client = New-Object Net.Sockets.TcpClient
            $connected = $false
            try {
                $attempt = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
                try {
                    if ($attempt.AsyncWaitHandle.WaitOne(350)) {
                        $client.EndConnect($attempt)
                        $connected = $client.Connected
                    }
                } finally { $attempt.AsyncWaitHandle.Close() }
            } catch { } finally { $client.Close() }
            [pscustomobject]@{ Port = $Port; TcpReachable = $connected }
        }

        function Test-SiwxProcessRecord($Record, [string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(8787, 8887)][int]$Port = 8787) {
            if (-not $Record -or -not $Record.Created -or $Record.Name -ine 'siwx-lite.exe' -or
                $Record.Sid -ne $UserSid -or $Record.Session -ne $SessionId) { return $false }
            if (-not (Test-SiwxTrustedInstall $Record.Path $ExePath)) { return $false }
            $pathPattern = [regex]::Escape($Record.Path)
            $bindPattern = '127\.0\.0\.1'
            if (-not [StringComparer]::OrdinalIgnoreCase.Equals($Record.Path, $ExePath)) {
                $bindPattern = '(?:127\.0\.0\.1|0\.0\.0\.0)'
            }
            $pattern = '^(?:"' + $pathPattern + '"|' + $pathPattern + ')\s+--host\s+' + $bindPattern + '\s+--port\s+' + $Port + '\s+--no-open(?:\s+--data\s+"[^"%\r\n]+")?\s*$'
            return $Record.Command -imatch $pattern
        }

        function Open-SiwxProcessHandle($Record, [string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(8787, 8887)][int]$Port = 8787) {
            $fresh = Get-SiwxProcessRecord $Record.Id
            if (-not (Test-SiwxProcessRecord $fresh $ExePath $UserSid $SessionId $Port) -or
                $fresh.Created -ne $Record.Created) { throw (ConvertFrom-ConsoleText '\u8fdb\u7a0b\u8eab\u4efd\u5df2\u53d8\u5316\uff0c\u672a\u7ed3\u675f\u4efb\u4f55\u8fdb\u7a0b\u3002') }
            $handle = [Diagnostics.Process]::GetProcessById($Record.Id)
            try {
                $null = $handle.Handle
                if ([Math]::Abs(($handle.StartTime.ToUniversalTime() - $Record.Created.ToUniversalTime()).TotalSeconds) -ge 1) {
                    throw (ConvertFrom-ConsoleText '\u8fdb\u7a0b\u542f\u52a8\u65f6\u95f4\u53d8\u5316\uff0c\u62d2\u7edd\u64cd\u4f5c\u5df2\u590d\u7528\u7684PID\u3002')
                }
                return $handle
            } catch { $handle.Dispose(); throw }
        }

        function Stop-SiwxProcessHandle($Handle) {
            if (-not $Handle.HasExited) { $Handle.Kill() }
            if (-not $Handle.WaitForExit(5000)) { throw (ConvertFrom-ConsoleText 'SIWX\u8fdb\u7a0b\u672a\u80fd\u9000\u51fa\u3002') }
        }

        function Stop-SiwxPortOwner([string]$ExePath, [string]$UserSid, [int]$SessionId, [ValidateRange(8787, 8887)][int]$Port = 8787) {
            $handles = @()
            try {
                $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop)
                if ($connections.Count -eq 0) { return $true }
                $probe = Get-SiwxPortProbe $Port
                Write-Host ('Port ' + $Port + ' TCP reachable=' + $probe.TcpReachable + '; validating SIWX file, owner and process identity.')
                $ids = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
                $records = @{}
                foreach ($processId in $ids) {
                    $record = Get-SiwxProcessRecord $processId
                    if (-not (Test-SiwxProcessRecord $record $ExePath $UserSid $SessionId $Port)) {
                        Write-Host ((ConvertFrom-ConsoleText "$Port\u7aef\u53e3\u5360\u7528PID={0}\uff1a\u65e0\u6cd5\u786e\u8ba4\u662f\u5f53\u524d\u7528\u6237\u7684SIWX\uff0c\u5df2\u4fdd\u7559\u3002") -f $processId)
                        return $false
                    }
                    $records[$record.Id] = $record
                    # PyInstaller one-file has an identical parent supervising the listening child.
                    $parent = Get-SiwxProcessRecord $record.ParentId
                    if ($parent -and $parent.Name -ieq 'siwx-lite.exe') {
                        if (-not (Test-SiwxProcessRecord $parent $ExePath $UserSid $SessionId $Port) -or
                            $parent.Created -gt $record.Created) {
                            throw (ConvertFrom-ConsoleText '\u65e0\u6cd5\u786e\u8ba4SIWX\u7236\u8fdb\u7a0b\u8eab\u4efd\uff0c\u672a\u7ed3\u675f\u4efb\u4f55\u8fdb\u7a0b\u3002')
                        }
                        $records[$parent.Id] = $parent
                    }
                }
                # Acquire and validate every original process handle before stopping any process.
                foreach ($record in @($records.Values | Sort-Object Created)) {
                    $handles += Open-SiwxProcessHandle $record $ExePath $UserSid $SessionId $Port
                }
                foreach ($task in @(Get-SiwxBackgroundTasks $UserSid | Where-Object { $_.Port -eq $Port -and $_.Task.State -in @('Running', 'Queued') })) {
                    Stop-ScheduledTask -TaskName $task.Task.TaskName -TaskPath $task.Task.TaskPath -ErrorAction Stop
                }
                foreach ($handle in $handles) { Stop-SiwxProcessHandle $handle }
                for ($attempt = 0; $attempt -lt 20; $attempt++) {
                    if (-not (Test-SiwxPort $Port)) {
                        Write-Host (ConvertFrom-ConsoleText "\u5df2\u7ed3\u675f\u786e\u8ba4\u5f52\u5c5e\u7684SIWX\u6b8b\u7559\u670d\u52a1\uff0c$Port \u7aef\u53e3\u5df2\u91ca\u653e\u3002")
                        return $true
                    }
                    Start-Sleep -Milliseconds 200
                }
                throw (ConvertFrom-ConsoleText "$Port \u7aef\u53e3\u4ecd\u88ab\u5360\u7528\u3002")
            } catch {
                Write-Host ((ConvertFrom-ConsoleText "\u672a\u80fd\u91ca\u653e $Port \u7aef\u53e3\uff1a") + $_.Exception.Message)
                return $false
            } finally {
                foreach ($handle in $handles) { $handle.Dispose() }
            }
        }

        function Test-SiwxHttp([ValidateRange(8787, 8887)][int]$Port = 8787) {
            try {
                $response = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:$Port/") -TimeoutSec 2
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
                    if ($meta.Kind -ne 'SiwxLiteOriginal344' -or $meta.UserSid -ne $UserSid) { continue }
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
                    $port = 8787
                    if ($meta.PSObject.Properties['Port']) {
                        if ([string]$meta.Port -notmatch '^88[0-9]{2}$|^87[89][0-9]$' -or
                            [int]$meta.Port -lt 8787 -or [int]$meta.Port -gt 8887) { continue }
                        $port = [int]$meta.Port
                    }
                    [pscustomobject]@{
                        Task = $task; LogPath = $log; Port = $port
                    }
                } catch { continue }
            }
        }

        function New-SiwxCmdWorker(
            [string]$ExePath, [string]$Sid, [string]$LogPath,
            [string]$ResultPath, [string]$ArgumentLine, [bool]$Probe, [string]$RootPath, [string]$DataDirectory
        ) {
            foreach ($path in @($ExePath, $LogPath, $ResultPath, $DataDirectory)) {
                if ($path -match '[%"\r\n]') {
                    throw (ConvertFrom-ConsoleText 'CMD \u5de5\u4f5c\u8def\u5f84\u4e0d\u80fd\u5305\u542b\u767e\u5206\u53f7\u3001\u53cc\u5f15\u53f7\u6216\u6362\u884c\u3002')
                }
            }
            if ($Sid -notmatch '^S-1-[0-9-]+$') { throw (ConvertFrom-ConsoleText '\u7528\u6237 SID \u65e0\u6548\u3002') }
            if ($ArgumentLine -notmatch '^(--version|--host 127\.0\.0\.1 --port (?:878[7-9]|879[0-9]|88[0-7][0-9]|888[0-7]) --no-open|probe)$') {
                throw (ConvertFrom-ConsoleText '\u4e0d\u652f\u6301\u6b64\u547d\u4ee4\u53c2\u6570\u3002')
            }
            if (-not $RootPath -or $RootPath -match '[%"\r\n]') { throw (ConvertFrom-ConsoleText '\u8fd0\u884c\u6839\u76ee\u5f55\u5305\u542b\u4e0d\u5b89\u5168\u5b57\u7b26\u3002') }
            if ($DataDirectory -and -not $Probe -and $ArgumentLine -ne '--version') {
                $ArgumentLine += ' --data "' + $DataDirectory + '"'
            }
            $directory = $RootPath
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

        function Find-SiwxDataFolders([string[]]$Roots) {
            foreach ($searchRoot in $Roots) {
                Get-ChildItem -LiteralPath $searchRoot -Directory -Filter 'xwechat_files' -Recurse -Force -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty FullName
            }
        }

        $nextCommand = 'p'
        while ($true) {
            if (-not $nextCommand) {
                Write-Host (ConvertFrom-ConsoleText "`n1 = \u73af\u5883\u548c\u7248\u672c\u68c0\u67e5   2 = \u6253\u5f00\u7f51\u9875\u63d0\u53d6\u3001\u67e5\u770b\u4e0e\u5bfc\u51fa")
                Write-Host (ConvertFrom-ConsoleText '6 = \u542f\u52a8\u7f51\u9875\u5e76\u663e\u793a\u65e5\u5fd7   7 = \u540e\u53f0\u542f\u52a8\u7f51\u9875')
                Write-Host (ConvertFrom-ConsoleText '8 = \u540e\u53f0\u72b6\u6001 / \u7f51\u5740 / \u65e5\u5fd7   9 = \u505c\u6b62\u540e\u53f0\u7f51\u9875   0 = \u9000\u51fa')
                Write-Host (ConvertFrom-ConsoleText '12 = \u5168\u76d8\u641c\u7d22 xwechat_files \u6587\u4ef6\u5939   13 = \u4ec5\u641c\u7d22 C\u76d8')
                Write-Host (ConvertFrom-ConsoleText '14 = \u8bbe\u7f6e / \u6e05\u9664\u8d26\u53f7\u6570\u636e\u76ee\u5f55   15 = \u8bbe\u7f6e\u9996\u9009\u7aef\u53e3')
                Write-Host (ConvertFrom-ConsoleText '16 = \u68c0\u6d4b\u5e76\u91ca\u653e\u9996\u9009\u7aef\u53e3\uff08\u4ec5\u786e\u8ba4\u5f52\u5c5e\u7684 SIWX\uff09')
                $nextCommand = Read-Host (ConvertFrom-ConsoleText '\u8bf7\u9009\u62e9')
            }
            if ($nextCommand -eq '0') { break }
            if ($nextCommand -eq '16') {
                $probe = Get-SiwxPortProbe $preferredPort
                Write-Host ('Port ' + $preferredPort + ' TCP reachable=' + $probe.TcpReachable)
                if (Test-SiwxPort $preferredPort) { $null = Stop-SiwxPortOwner $exePath $sid ([int]$sessions[0]) $preferredPort }
                else { Write-Host 'The preferred port is available.' }
                $nextCommand = $null; continue
            }
            if ($nextCommand -eq '14') {
                $candidate = Read-Host 'Account directory containing db_storage (empty = auto)'
                if ($candidate) {
                    Assert-SiwxLocalPath $candidate
                    if (-not (Test-Path -LiteralPath (Join-Path $candidate 'db_storage') -PathType Container)) {
                        Write-Host 'Not an account directory; previous selection preserved.'
                        $nextCommand = $null; continue
                    }
                }
                $dataDirectory = $candidate
                $nextCommand = $null; continue
            }
            if ($nextCommand -eq '15') {
                $candidatePort = 0
                $answer = Read-Host 'Preferred port (8787-8887)'
                if ([int]::TryParse($answer, [ref]$candidatePort) -and $candidatePort -ge 8787 -and $candidatePort -le 8887) {
                    $preferredPort = $candidatePort
                } else { Write-Host 'Invalid port; previous selection preserved.' }
                $nextCommand = $null; continue
            }
            if ($nextCommand -in @('12', '13')) {
                $searchRoots = @('C:\')
                if ($nextCommand -eq '12') {
                    $searchRoots = @(Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Root -Unique)
                }
                Write-Host (ConvertFrom-ConsoleText '\u6b63\u5728\u641c\u7d22 xwechat_files\uff0c\u78c1\u76d8\u8f83\u5927\u65f6\u53ef\u80fd\u9700\u8981\u8f83\u957f\u65f6\u95f4\uff1b\u65e0\u6743\u9650\u7684\u76ee\u5f55\u4f1a\u8df3\u8fc7\u3002\u641c\u7d22\u53ea\u8bfb\uff0c\u8def\u5f84\u4ec5\u663e\u793a\u5728\u5f53\u524d\u7a97\u53e3\u3002')
                $foundCount = 0
                Find-SiwxDataFolders $searchRoots | ForEach-Object {
                    Write-Host $_
                    $foundCount++
                }
                Write-Host ((ConvertFrom-ConsoleText '\u641c\u7d22\u5b8c\u6210\uff1a\u627e\u5230 {0} \u4e2a\u6587\u4ef6\u5939\uff08\u65e0\u6743\u9650\u6216\u4e0d\u53ef\u8fbe\u7684\u76ee\u5f55\u672a\u8ba1\u5165\uff09\u3002') -f $foundCount)
                $nextCommand = $null
                continue
            }
            if ($nextCommand -in @('8', '9')) {
                $backgroundTasks = @(Get-SiwxBackgroundTasks $sid)
                if ($backgroundTasks.Count -eq 0) { Write-Host (ConvertFrom-ConsoleText '\u8be5\u7528\u6237\u6ca1\u6709\u672c\u811a\u672c\u7ba1\u7406\u7684\u540e\u53f0\u4efb\u52a1\u3002') }
                for ($index = 0; $index -lt $backgroundTasks.Count; $index++) {
                    $record = $backgroundTasks[$index]
                    Write-Host ((ConvertFrom-ConsoleText "[{0}] {1}  \u72b6\u6001={2}") -f ($index + 1), $record.Task.TaskName, $record.Task.State)
                    Write-Host (ConvertFrom-ConsoleText "\u7f51\u5740\uff1ahttp://127.0.0.1:$($record.Port)/\uff08\u4ec5\u5728\u5bf9\u5e94\u670d\u52a1\u8fd0\u884c\u65f6\u6709\u6548\uff09")
                    Write-Host (ConvertFrom-ConsoleText "\u65e5\u5fd7\uff1a$($record.LogPath)")
                    if ($nextCommand -eq '8' -and (Test-Path -LiteralPath $record.LogPath)) {
                        Show-SiwxLogTail $record.LogPath 20
                    }
                }
                if ($nextCommand -eq '9' -and $backgroundTasks.Count -eq 0) {
                    foreach ($port in @(Get-SiwxListeningPorts)) {
                        $null = Stop-SiwxPortOwner $exePath $sid ([int]$sessions[0]) $port
                    }
                }
                if ($nextCommand -eq '9' -and $backgroundTasks.Count -gt 0) {
                    $selection = 0
                    $answer = Read-Host (ConvertFrom-ConsoleText '\u8bf7\u8f93\u5165\u8981\u505c\u6b62\u7684\u4efb\u52a1\u7f16\u53f7\uff08\u76f4\u63a5\u56de\u8f66\u53d6\u6d88\uff09')
                    if ([int]::TryParse($answer, [ref]$selection) -and $selection -ge 1 -and $selection -le $backgroundTasks.Count) {
                        $selectedPort = $backgroundTasks[$selection - 1].Port
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
                        if (Test-SiwxPort $selectedPort) { $null = Stop-SiwxPortOwner $exePath $sid ([int]$sessions[0]) $selectedPort }
                    }
                }
                $nextCommand = $null
                continue
            }
            $isProbe = $nextCommand -eq 'p'
            $backgroundMode = $nextCommand -in @('7', '11')
            $isServe = $nextCommand -in @('2', '6', '7', '10', '11')
            # All SIWX operations share the same cache/output root. Avoid concurrent writers.
            if (-not $isProbe -and -not $isServe -and @(Get-SiwxBackgroundTasks $sid | Where-Object {
                $_.Task.State -in @('Running', 'Queued') }).Count -gt 0) {
                Write-Host (ConvertFrom-ConsoleText '\u5df2\u6709\u540e\u53f0\u7f51\u9875\u6b63\u5728\u8fd0\u884c\uff0c\u8bf7\u4f7f\u7528\u8be5\u7f51\u9875\uff0c\u6216\u7528 8 \u67e5\u770b\u30019 \u505c\u6b62\u3002')
                $nextCommand = $null
                continue
            }
            $siwxArguments = switch ($nextCommand) {
                'p' { 'probe' }
                '1' { '--version' }
                { $_ -in @('2', '6', '7', '10', '11') } { '--host 127.0.0.1 --port 8787 --no-open' }
            }
            if (-not $siwxArguments) { $nextCommand = $null; continue }
            $webPort = 8787
            if ($isServe) {
                $active = @(Get-SiwxBackgroundTasks $sid | Where-Object { $_.Task.State -in @('Running', 'Queued') })
                if ($active.Count -gt 0) { $preferredPort = $active[0].Port }
                $webPort = Select-SiwxPort $exePath $sid ([int]$sessions[0]) $preferredPort
                $siwxArguments = $siwxArguments.Replace('--port 8787 ', ('--port ' + $webPort + ' '))
                if ($webPort -ne 8787) {
                    Write-Host (ConvertFrom-ConsoleText "\u9ed8\u8ba4\u7aef\u53e3\u88ab\u5360\u7528\uff0c\u5df2\u9009\u62e9\u7aef\u53e3 $webPort\uff1b\u53cd\u4ee3/\u5185\u7f51\u7a7f\u900f\u7684\u670d\u52a1\u7aef\u53e3\u4e5f\u9700\u6539\u4e3a $webPort\u3002")
                }
            }
            $nextCommand = $null
            Assert-SiwxLocalPath $rootPath
            Assert-SiwxInstallAcl $installPath
            Assert-SiwxInstallAcl $exePath
            Assert-SiwxDistribution $installPath $manifestHash
            $jobId = [Guid]::NewGuid().ToString('N')
            $logPath = Join-Path $bridgeDirectory "$jobId.log"
            $resultPath = Join-Path $bridgeDirectory "$jobId.result"
            $workerPath = Join-Path $bridgeDirectory "$jobId.cmd"
            $worker = New-SiwxCmdWorker $exePath $sid $logPath $resultPath $siwxArguments $isProbe $rootPath $dataDirectory
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
                    Kind = 'SiwxLiteOriginal344'; UserSid = $sid
                    LogPath = $logPath; Port = $webPort
                } | ConvertTo-Json -Compress
            }
            $action = New-ScheduledTaskAction -Execute $scriptHost `
                -Argument ('//B //Nologo //E:JScript "' + $launcherPath + '"') `
                -WorkingDirectory $bridgeDirectory
            $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
            $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $currentTask -Action $action -Principal $principal `
                -Settings $settings -Description $description -ErrorAction Stop | Out-Null
            Write-Host (ConvertFrom-ConsoleText "`n\u6b63\u5728\u4ee5 $userId \u8eab\u4efd\u8fd0\u884c\uff0c\u4ee5\u4e0b\u4e3a\u8be5\u7528\u6237\u7684\u7a0b\u5e8f\u8f93\u51fa\u3002")
            Write-Host (ConvertFrom-ConsoleText "\u8bca\u65ad\u4efb\u52a1\uff1a$currentTask")
            if ($isServe) { Write-Host ('Listening 127.0.0.1:' + $webPort + ' / no authentication; use localhost or a proxy client on this computer.') }
            if ($isProbe) { Write-Host (ConvertFrom-ConsoleText '\u6b63\u5728\u68c0\u67e5\u7528\u6237\u4f1a\u8bdd CMD \u540e\u7aef\uff0c\u901a\u8fc7\u540e\u624d\u4f1a\u8fd0\u884c siwx.exe\u2026\u2026') }
            Write-Host (ConvertFrom-ConsoleText "\u6309 Q \u505c\u6b62\u5e76\u8fd4\u56de\u83dc\u5355\uff0cCtrl+C \u9000\u51fa\u3002\u7f51\u9875\u5730\u5740\uff1ahttp://127.0.0.1:$webPort/")
            $interactiveKeyboard = -not [Console]::IsInputRedirected
            Start-ScheduledTask -TaskName $currentTask -ErrorAction Stop
            $position = [long]0
            $started = Get-Date
            $stopRequested = $false
            $detached = $false
            $savedUrl = $null
            $stoppedAt = $null
            while ($true) {
                Show-SiwxOutput $logPath ([ref]$position)
                if (Test-Path -LiteralPath $resultPath) { break }
                if ($isServe -and -not $savedUrl -and (Test-SiwxHttp $webPort)) {
                    $savedUrl = "http://127.0.0.1:$webPort/"
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
