#Requires -Version 5.1
param([string]$Distribution, [string]$DistributionManifestHash)
$ErrorActionPreference = 'Stop'
$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'Siwx-LiteConsole.ps1'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
foreach($name in @('ConvertFrom-ConsoleText','Assert-SiwxPlainPath','Assert-SiwxDistribution','Get-SiwxPayload','Install-SiwxRelease','New-SiwxCmdWorker')){
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    if(-not $node){throw ('Missing function: '+$name)}
    . ([ScriptBlock]::Create($node.Extent.Text))
}
function Must-Fail([scriptblock]$Action){
    $failed=$false
    try{& $Action}catch{$failed=$true}
    if(-not $failed){throw 'Expected rejection'}
}
$temp=Join-Path (Get-Location) ('.siwx-lite-test-'+[Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temp|Out-Null
try{
    # Only ACL operations and transport are mocked. Real ZIP, stage, manifest and hash paths run.
    function New-SiwxInstallDirectory([string]$Path){New-Item -ItemType Directory -Path $Path|Out-Null}
    function Assert-SiwxInstallAcl([string]$Path){$script:aclChecked+=@($Path)}
    $payload=Join-Path $temp 'payload';New-Item -ItemType Directory -Path $payload|Out-Null
    $exe=Join-Path $payload 'siwx-lite.exe';[IO.File]::WriteAllText($exe,'synthetic-not-an-executable')
    $hash=(Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
    $manifest=Join-Path $payload 'SHA256SUMS.txt'
    [IO.File]::WriteAllText($manifest,($hash+'  siwx-lite.exe'+"`n"))
    $manifestHash=(Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    Assert-SiwxDistribution $payload $manifestHash
    $script:fixtureZip=Join-Path $temp 'fixture.zip';Compress-Archive -Path (Join-Path $payload '*') -DestinationPath $script:fixtureZip
    $zipHash=(Get-FileHash -LiteralPath $script:fixtureZip -Algorithm SHA256).Hash
    function Invoke-WebRequest {param($Uri,$OutFile,$TimeoutSec,[switch]$UseBasicParsing)
        if($script:networkFails){throw 'Synthetic network failure'}
        Copy-Item -LiteralPath $script:fixtureZip -Destination $OutFile
    }
    $target=Join-Path $temp 'installed';$script:aclChecked=@()
    Install-SiwxRelease $target 'https://example.invalid/fixture.zip' $zipHash $manifestHash
    if($script:aclChecked.Count -ne 3){throw 'Install ACL verification did not cover distribution/files'}
    Install-SiwxRelease $target 'https://example.invalid/fixture.zip' $zipHash $manifestHash
    [IO.File]::WriteAllText((Join-Path $target 'extra.dll'),'unexpected')
    Must-Fail { Install-SiwxRelease $target 'https://example.invalid/fixture.zip' $zipHash $manifestHash }
    Remove-Item -LiteralPath (Join-Path $target 'extra.dll')
    [IO.File]::WriteAllText((Join-Path $target 'siwx-lite.exe'),'tampered')
    Must-Fail { Install-SiwxRelease $target 'https://example.invalid/fixture.zip' $zipHash $manifestHash }
    if([IO.File]::ReadAllText((Join-Path $target 'siwx-lite.exe')) -ne 'tampered'){throw 'Existing content overwritten'}
    foreach($failure in @('hash','manifest','network')){
        $destination=Join-Path $temp $failure
        $script:networkFails=$failure -eq 'network'
        $expectedZip=$zipHash;$expectedManifest=$manifestHash
        if($failure -eq 'hash'){$expectedZip='0'*64}
        if($failure -eq 'manifest'){$expectedManifest='0'*64}
        Must-Fail { Install-SiwxRelease $destination 'https://example.invalid/fixture.zip' $expectedZip $expectedManifest }
        if((Test-Path -LiteralPath $destination) -or @(Get-ChildItem -LiteralPath $temp -Filter ($failure+'.*')).Count){throw 'Failed install left stage/ZIP/destination'}
    }
    $script:networkFails=$false
    [IO.File]::WriteAllText($manifest,($hash+'  ..\outside.exe'+"`n"))
    $unsafeHash=(Get-FileHash -LiteralPath $manifest -Algorithm SHA256).Hash
    Must-Fail {Assert-SiwxDistribution $payload $unsafeHash}
    $link=Join-Path $temp 'link';New-Item -ItemType SymbolicLink -Path $link -Target $payload|Out-Null
    Must-Fail {Assert-SiwxPlainPath $link}
    $cmd=New-SiwxCmdWorker 'C:\Tools\siwx-lite.exe' 'S-1-5-21-1-1001' 'C:\Temp\job.log' 'C:\Temp\job.result' '--host 0.0.0.0 --port 8789 --no-open' $false 'C:\Temp\root' 'D:\example & !{LOG}\user_a'
    if(-not $cmd.Contains('--host 0.0.0.0 --port 8789 --no-open --data "D:\example & !{LOG}\user_a" <nul')){throw 'Data argument quoting/forwarding failed'}
    foreach($bad in @('D:\bad%PATH%','D:\bad"path',"D:\bad`npath")){
        Must-Fail {New-SiwxCmdWorker 'C:\Tools\siwx-lite.exe' 'S-1-5-21-1-1001' 'C:\Temp\j.log' 'C:\Temp\j.result' '--host 0.0.0.0 --port 8787 --no-open' $false 'C:\Temp\root' $bad}
    }
    if($Distribution){Assert-SiwxDistribution $Distribution $DistributionManifestHash}
    Write-Output 'PASS: real ZIP install/reuse, full manifest/files, tamper/extra/traversal/symlink rejection, hash/manifest/network failure cleanup, optional data quoting; no EXE execution or real ACL changes'
}finally{if(Test-Path -LiteralPath $temp){Remove-Item -LiteralPath $temp -Recurse -Force}}
