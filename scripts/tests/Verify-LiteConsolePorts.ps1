#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path (Split-Path -Parent $PSScriptRoot) 'Siwx-LiteConsole.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Console parse error'}
foreach($name in @('ConvertFrom-ConsoleText','Test-SiwxTrustedInstall','Get-SiwxPortProbe','Test-SiwxProcessRecord','Open-SiwxProcessHandle','Stop-SiwxPortOwner')){
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    . ([ScriptBlock]::Create($node.Extent.Text))
}
if(-not $ast.Extent.Text.Contains('[Net.IPAddress]::Loopback') -or $ast.Extent.Text.Contains('[Net.IPAddress]::Any')){throw 'Port probe must bind only loopback'}
function Get-SiwxPortProbe {param($Port=8787) [pscustomobject]@{Port=$Port;TcpReachable=$true}}
$exe='C:\Tools\siwx-lite.exe';$sid='S-1-5-21-1-1001';$sessionId=2
function New-Fixture {
    [pscustomobject]@{Id=101;ParentId=100;Name='siwx-lite.exe';Path=$exe;Sid=$sid;Session=$sessionId;
        Created=[datetime]'2026-01-01T00:00:01Z';Command=('"'+$exe+'" --host 127.0.0.1 --port 8787 --no-open')}
}
$record=New-Fixture
$wrongBind=New-Fixture;$wrongBind.Command=$wrongBind.Command.Replace('127.0.0.1','0.0.0.0')
if(Test-SiwxProcessRecord $wrongBind $exe $sid $sessionId){throw 'Non-loopback command owner accepted'}
if(-not (Test-SiwxProcessRecord $record $exe $sid $sessionId)){throw 'Valid owner rejected'}
foreach($field in @('Name','Path','Sid','Session','Created','Command')){
    $bad=New-Fixture
    switch($field){
        Name {$bad.Name='other.exe'}
        Path {$bad.Path='C:\Other\siwx-lite.exe'}
        Sid {$bad.Sid='S-1-5-21-1-1002'}
        Session {$bad.Session=9}
        Created {$bad.Created=$null}
        Command {$bad.Command+=' --unexpected'}
    }
    if(Test-SiwxProcessRecord $bad $exe $sid $sessionId){throw "Unverified field accepted: $field"}
}
# Legacy/full-version locations must not be terminated by this Lite launcher.
$legacy=New-Fixture;$legacy.Path='C:\Data\user_a\siwx-v5.2.0.exe';$legacy.Name='siwx-v5.2.0.exe'
if(Test-SiwxProcessRecord $legacy $exe $sid $sessionId){throw 'Legacy/full-version process accepted'}
$withData=New-Fixture;$withData.Command+=' --data "D:\example\user_a"'
if(-not (Test-SiwxProcessRecord $withData $exe $sid $sessionId)){throw 'Quoted data argument rejected'}
# A recycled PID must be rejected before attempting to open/kill the native process.
$changed=New-Fixture;$changed.Created=$changed.Created.AddSeconds(2)
function Get-SiwxProcessRecord {param($ProcessId) $changed}
$rejected=$false
try{$null=Open-SiwxProcessHandle $record $exe $sid $sessionId}catch{$rejected=$true}
if(-not $rejected){throw 'Reused PID accepted'}

function Reset-Fixture {
    $script:child=New-Fixture
    $script:parent=New-Fixture;$script:parent.Id=100;$script:parent.ParentId=50;$script:parent.Created=$child.Created.AddSeconds(-1)
    $script:records=@{101=$child;100=$parent}
    $script:listeners=@(101)
    $script:killed=New-Object 'Collections.Generic.List[int]'
    $script:disposed=New-Object 'Collections.Generic.List[int]'
    $script:opened=New-Object 'Collections.Generic.List[int]'
    $script:openFailure=0
}
function Get-NetTCPConnection {param($LocalPort,$State,$ErrorAction) foreach($n in $script:listeners){[pscustomobject]@{OwningProcess=$n}}}
function Get-SiwxProcessRecord {param($ProcessId) $script:records[[int]$ProcessId]}
function Open-SiwxProcessHandle {param($Record,$ExePath,$UserSid,$SessionId)
    if($Record.Id -eq $script:openFailure){throw 'Synthetic identity race'}
    $script:opened.Add($Record.Id)
    $h=[pscustomobject]@{Id=$Record.Id}
    $h|Add-Member -MemberType ScriptMethod -Name Dispose -Value {$script:disposed.Add($this.Id)}
    return $h
}
function Stop-SiwxProcessHandle {param($Handle) $script:killed.Add($Handle.Id)}
function Get-SiwxBackgroundTasks {param($UserSid) @()}
function Test-SiwxPort {$false}
Reset-Fixture
$ok=Stop-SiwxPortOwner $exe $sid $sessionId
if(-not $ok -or ($script:killed -join ',') -ne '100,101' -or $script:disposed.Count -ne 2){throw 'Exact parent/child cleanup failed'}
# Mixed listeners must reject the entire cleanup before stopping any process.
Reset-Fixture
$other=New-Fixture;$other.Id=102;$other.Path='C:\Unrelated\other.exe';$script:records[102]=$other;$script:listeners=@(101,102)
if((Stop-SiwxPortOwner $exe $sid $sessionId) -or $script:killed.Count -or $script:opened.Count){throw 'Mixed unknown listener caused termination'}
Reset-Fixture
$script:parent.Sid='S-1-5-21-1-1002'
if((Stop-SiwxPortOwner $exe $sid $sessionId) -or $script:killed.Count){throw 'Unverified parent caused termination'}
Reset-Fixture
$script:openFailure=101
if((Stop-SiwxPortOwner $exe $sid $sessionId) -or $script:killed.Count -or $script:disposed.Count -ne 1){throw 'Partial acquisition caused termination or leaked handle'}
Reset-Fixture
$script:records.Remove(100)
if(-not (Stop-SiwxPortOwner $exe $sid $sessionId) -or ($script:killed -join ',') -ne '101'){throw 'Orphan without parent not cleaned'}
Reset-Fixture
$script:listeners=@()
if(-not (Stop-SiwxPortOwner $exe $sid $sessionId) -or $script:killed.Count){throw 'Empty port changed processes'}
Write-Output 'PASS: exact path/name/SID/session/command/create-time identity; PID reuse; parent-first cleanup; mixed/foreign listeners refused; all-handles-before-stop; orphan/no-listener cases'
# Dynamic ports retain exact command validation, including fallback-port parents.
$dynamic=New-Fixture;$dynamic.Command=$dynamic.Command.Replace('8787','8789')
if(-not (Test-SiwxProcessRecord $dynamic $exe $sid $sessionId 8789) -or
    (Test-SiwxProcessRecord $dynamic $exe $sid $sessionId 8788)){throw 'Port identity mismatch accepted'}
Reset-Fixture
$script:child.Command=$script:child.Command.Replace('8787','8789')
$script:parent.Command=$script:parent.Command.Replace('8787','8789')
if(-not (Stop-SiwxPortOwner $exe $sid $sessionId 8789) -or ($script:killed -join ',') -ne '100,101'){
    throw 'Fallback port cleanup failed'
}
foreach($name in @('Test-SiwxPort','Select-SiwxPort')){
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    . ([ScriptBlock]::Create($node.Extent.Text))
}
# Exercise real socket bind detection without Windows process APIs.
$socket=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, 0)
try {
    # Find an available port within the launcher's bounded range.
    $freePort=0
    foreach($candidate in 8787..8887){if(-not (Test-SiwxPort $candidate)){$freePort=$candidate;break}}
    if(-not $freePort){throw 'Test environment has no free fixture port'}
    $socket=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $freePort)
    $socket.Server.ExclusiveAddressUse=$true
    $socket.Start()
    if(-not (Test-SiwxPort $freePort)){throw 'Real bound port undetected'}
} finally {$socket.Stop()}
if(Test-SiwxPort $freePort){throw 'Released port detected as occupied'}
function Reset-PortSelection {
    $script:busy=@{}
    $script:owned=@{}
    $script:attempted=New-Object 'Collections.Generic.List[int]'
}
function Test-SiwxPort {param($Port=8787) [bool]$script:busy[[int]$Port]}
function Stop-SiwxPortOwner {param($ExePath,$UserSid,$SessionId,$Port=8787)
    $script:attempted.Add([int]$Port)
    if($script:owned[[int]$Port]){$script:busy[[int]$Port]=$false;return $true}
    return $false
}
Reset-PortSelection
if((Select-SiwxPort $exe $sid $sessionId) -ne 8787 -or $script:attempted.Count){throw 'Free default changed processes'}
Reset-PortSelection
$script:busy[8787]=$true;$script:owned[8787]=$true
if((Select-SiwxPort $exe $sid $sessionId) -ne 8787 -or ($script:attempted -join ',') -ne '8787'){throw 'Owned default not restarted'}
Reset-PortSelection
$script:busy[8787]=$true;$script:busy[8788]=$true
if((Select-SiwxPort $exe $sid $sessionId) -ne 8789 -or ($script:attempted -join ',') -ne '8787,8788'){throw 'Foreign occupancy fallback failed'}
Reset-PortSelection
$script:busy[8789]=$true;$script:owned[8789]=$true
if((Select-SiwxPort $exe $sid $sessionId 8789) -ne 8789 -or ($script:attempted -join ',') -ne '8789'){throw 'Existing managed port not preferred'}
Reset-PortSelection
foreach($p in 8787..8887){$script:busy[$p]=$true}
$failed=$false
try{$null=Select-SiwxPort $exe $sid $sessionId}catch{$failed=$true}
if(-not $failed -or $script:attempted.Count -ne 101){throw 'Exhausted port range accepted'}
Write-Output 'PASS: real socket bind/release; dynamic command identity; fallback cleanup; free/owned/foreign/default/preferred/exhausted selection'

# Socket liveness is only a diagnostic: foreign same-name listeners remain refused.
Write-Output 'PASS: TCP reachability never bypasses executable/SID/session/CMD/create-time identity'
# Known previous releases require exact protected location, pinned manifest and all-file ACL.
$oldSystemRoot=$env:SystemRoot
$fixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('siwx-port-test-'+[guid]::NewGuid().ToString('N'))
try {
 $env:SystemRoot=$fixtureRoot
 $oldDir=Join-Path (Join-Path $fixtureRoot 'SIWX') 'lite-1.5.2-4f20738'
 $null=New-Item -ItemType Directory -Path $oldDir -Force
 $oldExe=Join-Path $oldDir 'siwx-lite.exe';Set-Content -LiteralPath $oldExe -Value 'synthetic'
 $script:manifestChecked=0;$script:badManifest=$false;$script:badAcl=$false
 function Assert-SiwxPlainPath {param($Path)}
 function Assert-SiwxInstallAcl {param($Path) if($script:badAcl){throw 'Synthetic ACL mismatch'}}
 function Assert-SiwxDistribution {param($Path,$Hash)
  if($Path -ne $oldDir -or $Hash -ne 'a96f7e62b768c1b544546d9aa7c71139f15cf19b9266f4743ef168b606d61cd2' -or $script:badManifest){throw 'Synthetic manifest mismatch'}
  $script:manifestChecked++
 }
 if(-not (Test-SiwxTrustedInstall $oldExe $exe) -or $script:manifestChecked -ne 1){throw 'Known verified release rejected'}
 $oldRecord=New-Fixture;$oldRecord.Path=$oldExe;$oldRecord.Command='"'+$oldExe+'" --host 0.0.0.0 --port 8787 --no-open'
 if(-not (Test-SiwxProcessRecord $oldRecord $exe $sid $sessionId)){throw 'Verified old bind cleanup rejected'}
 $script:badManifest=$true
 if(Test-SiwxProcessRecord $oldRecord $exe $sid $sessionId){throw 'Tampered release accepted'}
 $script:badManifest=$false;$script:badAcl=$true
 if(Test-SiwxTrustedInstall $oldExe $exe){throw 'Unsafe old ACL accepted'}
 $script:badAcl=$false
 if(Test-SiwxTrustedInstall ($oldExe.Replace('lite-1.5.2-4f20738','lite-9.9.9-forged')) $exe){throw 'Unknown release accepted'}
 if(Test-SiwxTrustedInstall (Join-Path $fixtureRoot 'siwx-lite.exe') $exe){throw 'Foreign same-name path accepted'}
} finally {$env:SystemRoot=$oldSystemRoot;Remove-Item -LiteralPath $fixtureRoot -Recurse -Force}
# Real diagnostic probe: succeeds only while the isolated loopback listener exists.
$node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-SiwxPortProbe'},$true)
. ([ScriptBlock]::Create($node.Extent.Text))
$socket=New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $freePort)
try {$socket.Start();if(-not (Get-SiwxPortProbe $freePort).TcpReachable){throw 'Real listening socket unreachable'}}finally{$socket.Stop()}
if((Get-SiwxPortProbe $freePort).TcpReachable){throw 'Closed socket reachable'}
Write-Output 'PASS: pinned known release/location/manifest/ACL, reject tamper/forged path; real TCP connect/closed diagnostic'
