#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$DataDirectory,
    [ValidateSet('0.0.0.0', '127.0.0.1')][string]$ListenAddress = '0.0.0.0',
    [ValidateRange(0, 65535)][int]$Port = 8787
)

# Task339 / 2026-11-02: separate foreground test launcher, not the old SYSTEM bridge.
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

function Start-LiteMain {
    $ErrorActionPreference = 'Stop'
    if ($env:OS -ne 'Windows_NT') { throw 'Run in Windows PowerShell on your own desktop.' }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if ($identity.User.Value -in @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')) {
            throw (ConvertFrom-LiteText '\u8bf7\u5728\u672c\u4ebaWindows\u684c\u9762PowerShell\u8fd0\u884c\uff0c\u4e0d\u8981\u4f7f\u7528SYSTEM\u6216\u670d\u52a1\u7a97\u53e3\u3002')
        }
    } finally { $identity.Dispose() }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is unavailable.' }

    $zipHash = '6e892c238e0c19d51682e605a349f0903189637a993a2aeb69d4a9a50f16dc23'
    $manifestHash = 'a1279e49ee78c343cac7c9d28824c41a9bcca992d1d9d2b180032a458b35d438'
    $url = 'http://139.196.47.109:9939/AAAAAAAA/siwx-lite-1.2.0-492f397-windows-x64.zip'
    $root = Join-Path $env:LOCALAPPDATA 'SIWX-Lite'
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
    $exe = Join-Path $version 'siwx-lite.exe'
    $arguments = @('--host', $ListenAddress, '--port', [string]$Port)
    if (-not [string]::IsNullOrWhiteSpace($DataDirectory)) {
        $data = Get-Item -LiteralPath $DataDirectory -ErrorAction Stop
        if (-not $data.PSIsContainer -or -not (Test-Path -LiteralPath (Join-Path $data.FullName 'db_storage') -PathType Container)) {
            throw 'DataDirectory must contain db_storage.'
        }
        $arguments += @('--data', $data.FullName)
    }
    Write-Host (ConvertFrom-LiteText '\u6821\u9a8c\u901a\u8fc7\uff0c\u524d\u53f0\u542f\u52a8\u3002\u65e0\u4ee4\u724c\u65e0\u5bc6\u7801\uff0c\u80fd\u8fde\u63a5\u7aef\u53e3\u7684\u8bbe\u5907\u5747\u53ef\u64cd\u4f5c\u3002')
    Write-Host (ConvertFrom-LiteText '\u4fdd\u7559\u672c\u7a97\u53e3\uff0c\u624b\u52a8\u6253\u5f00\u7a0b\u5e8f\u8f93\u51fa\u7684\u7f51\u5740\uff1b\u7f51\u9875\u505c\u6b62\u6216Ctrl+C\u9000\u51fa\u3002')
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw ('SIWX Lite exited with code ' + $LASTEXITCODE) }
}

Start-LiteMain
