param([string]$ScriptPath = (Join-Path $PSScriptRoot '..\Start-SiwxLite.ps1'), [string]$Distribution)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $ScriptPath), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load only named helper definitions; never call Start-LiteMain or execute the EXE.
foreach ($name in @('ConvertFrom-LiteText', 'Assert-LitePlainPath', 'Assert-LiteDistribution', 'Get-LitePayload')) {
    $node = @($ast.FindAll({ param($item) $item -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | Where-Object Name -eq $name)
    if ($node.Count -ne 1) { throw ('Missing helper: ' + $name) }
    Invoke-Expression $node[0].Extent.Text
}
function Must-Fail([scriptblock]$Action) {
    $failed = $false
    try { & $Action } catch { $failed = $true }
    if (-not $failed) { throw 'Expected operation to fail.' }
}
if ((ConvertFrom-LiteText '\u4e2d\u6587') -ne ([string][char]0x4e2d + [char]0x6587)) { throw 'Chinese prompt decoding failed.' }
$temp = Join-Path (Get-Location).Path ('.siwx-lite-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $payload = Join-Path $temp 'payload'
    New-Item -ItemType Directory -Path $payload | Out-Null
    $exe = Join-Path $payload 'siwx-lite.exe'
    [IO.File]::WriteAllText($exe, 'synthetic-not-an-executable')
    $hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    $manifest = Join-Path $payload 'SHA256SUMS.txt'
    [IO.File]::WriteAllText($manifest, ($hash + '  siwx-lite.exe' + "`n"))
    $manifestHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    Assert-LiteDistribution $payload $manifestHash
    [IO.File]::WriteAllText((Join-Path $payload 'extra.dll'), 'unexpected')
    Must-Fail { Assert-LiteDistribution $payload $manifestHash }
    Remove-Item -LiteralPath (Join-Path $payload 'extra.dll')
    [IO.File]::WriteAllText($exe, 'tampered')
    Must-Fail { Assert-LiteDistribution $payload $manifestHash }
    [IO.File]::WriteAllText($manifest, ($hash + '  ..\outside.exe' + "`n"))
    $unsafeHash = (Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    Must-Fail { Assert-LiteDistribution $payload $unsafeHash }
    Must-Fail { Assert-LiteDistribution $payload $manifestHash }
    $link = Join-Path $temp 'link'
    New-Item -ItemType SymbolicLink -Path $link -Target $payload | Out-Null
    Must-Fail { Assert-LitePlainPath $link }
    function Invoke-WebRequest { param($Uri, $OutFile, $TimeoutSec, [switch]$UseBasicParsing) [IO.File]::WriteAllText($OutFile, 'synthetic-download') }
    $download = Join-Path $temp 'download.zip'
    Must-Fail { Get-LitePayload 'https://example.invalid/not-called' $download ('0' * 64) }
    $downloadHash = (Get-FileHash -LiteralPath $download -Algorithm SHA256).Hash
    Get-LitePayload 'https://example.invalid/not-called' $download $downloadHash
    if ($Distribution) {
        Assert-LiteDistribution $Distribution 'a1279e49ee78c343cac7c9d28824c41a9bcca992d1d9d2b180032a458b35d438'
    }
    $source = Get-Content -LiteralPath $ScriptPath -Raw
    if ($source -match 'Set-MpPreference|Add-MpPreference|Set-ExecutionPolicy|schtasks|Register-ScheduledTask|EncodedCommand') {
        throw 'Unexpected system/security change in launcher.'
    }
    if (-not $source.Contains("'S-1-5-18', 'S-1-5-19', 'S-1-5-20'")) { throw 'Service-user rejection changed.' }
    if (-not $source.Contains("[string]`$ListenAddress = '0.0.0.0'")) { throw 'Default bind changed.' }
    Write-Output 'PASS: syntax, manifest/files, extra-file/tamper/traversal/reparse rejection, download hash gate; EXE never executed.'
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
