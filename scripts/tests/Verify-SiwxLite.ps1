param([string]$ScriptPath = (Join-Path $PSScriptRoot '..\Start-SiwxLite.ps1'), [string]$Distribution)
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $ScriptPath), [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
# Load only named helper definitions; never call Start-LiteMain or execute the EXE.
foreach ($name in @('ConvertFrom-LiteText', 'Assert-LitePlainPath', 'Assert-LiteDistribution', 'Get-LitePayload', 'Resolve-LiteDesktop', 'New-LiteUserWorker', 'Show-LiteUserLog', 'Get-LiteAccountSid', 'Test-LiteRegisteredTask', 'Start-LiteUserTask')) {
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
    if ($source -match 'Set-MpPreference|Add-MpPreference|Set-ExecutionPolicy|EncodedCommand|RunLevel Highest|New-ScheduledTaskTrigger') {
        throw 'Unexpected system/security change in launcher.'
    }
    if (-not $source.Contains("'S-1-5-18', 'S-1-5-19', 'S-1-5-20'")) { throw 'Service-user rejection changed.' }
    if (-not $source.Contains("[string]`$ListenAddress = '0.0.0.0'")) { throw 'Default bind changed.' }
    $one = @([pscustomobject]@{Sid='S-1-5-21-1-1001';Session=2}, [pscustomobject]@{Sid='S-1-5-21-1-1001';Session=2})
    if ((Resolve-LiteDesktop $one '').Session -ne 2) { throw 'Duplicate Explorer selection failed.' }
    Must-Fail { Resolve-LiteDesktop @() '' }
    $two = @($one[0], [pscustomobject]@{Sid='S-1-5-21-1-1002';Session=3})
    Must-Fail { Resolve-LiteDesktop $two '' }
    if ((Resolve-LiteDesktop $two 'S-1-5-21-1-1002').Session -ne 3) { throw 'Explicit desktop selection failed.' }
    Must-Fail { Resolve-LiteDesktop @($one[0], [pscustomobject]@{Sid='S-1-5-21-1-1001';Session=4}) '' }
    Must-Fail { Resolve-LiteDesktop @([pscustomobject]@{Sid='S-1-5-18';Session=1}) '' }
    $worker = New-LiteUserWorker 'C:\Example space & !\siwx-lite.exe' 'S-1-5-21-1-1001' 2 'C:\Example\output.log' 'C:\Example\exit.txt' '0.0.0.0' 8787 'D:\Example & !\account'
    if ($worker.IndexOf('if not "%ACTUAL_SID%"') -gt $worker.IndexOf('"C:\Example space & !\siwx-lite.exe" --host')) { throw 'Identity check comes after execution.' }
    if (-not $worker.Contains('--data "D:\Example & !\account"') -or -not $worker.Contains('DisableDelayedExpansion')) { throw 'Argument quoting failed.' }
    foreach ($bad in @('C:\bad%PATH%\app.exe', 'C:\bad" & whoami', "C:\bad`nline", '\\server\share\app.exe')) {
        Must-Fail { New-LiteUserWorker $bad 'S-1-5-21-1-1001' 2 'C:\Example\log' 'C:\Example\exit' '0.0.0.0' 8787 '' }
    }
    Must-Fail { New-LiteUserWorker 'C:\Example\app.exe' 'S-1-5-18' 2 'C:\Example\log' 'C:\Example\exit' '0.0.0.0' 8787 '' }
    $log = Join-Path $temp 'relay.log'; $position = [long]0
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($log, "line1`npartial", $utf8)
    $first = @(Show-LiteUserLog $log ([ref]$position) 6>&1)
    if ($position -ne 6 -or $first.Count -ne 1 -or $first[0].ToString() -ne "line1`n") { throw 'Relay cursor failed.' }
    [IO.File]::AppendAllText($log, " done`n", $utf8)
    $second = @(Show-LiteUserLog $log ([ref]$position) 6>&1)
    if ($second.Count -ne 1 -or $second[0].ToString() -ne "partial done`n") { throw 'Relay partial-line handling failed.' }
    if (-not $source.Contains('-LogonType Interactive -RunLevel Limited') -or
        -not $source.Contains('Unregister-ScheduledTask -TaskName $taskName')) { throw 'One-time Limited task controls missing.' }
    # Simulate scheduler/CIM/ACL APIs. Never create a real task or execute any process.
    $desktop = [pscustomobject]@{Sid='S-1-5-21-1-1001';Session=2}
    $ListenAddress = '0.0.0.0'; $Port = 8787; $DataDirectory = ''; $UserId = ''
    $script:registrations = 0; $script:unregistrations = 0; $script:stops = 0; $script:failDispatch = $false; $script:backgroundReady = $false
    $savedSystemRoot = $env:SystemRoot; $env:SystemRoot = 'C:\Windows'
    $script:failRegister=$false; $script:startCalls=0; $script:nullTask=$false; $script:badPrincipal=$false
    function Import-Module { param($Name,$ErrorAction) }
    function Get-LiteSchedulerUser { param($Sid) 'EXAMPLE\user_example' }
    function Get-LiteAccountSid { param($Account)
        if ($Account -eq 'EXAMPLE\user_example' -or $Account -eq $desktop.Sid) { return $desktop.Sid }
        throw 'Unknown or empty task account.' }
    function Get-LiteDesktop { param($RequestedUser) $desktop }
    function New-LiteControlledDirectory { param($Path, $ReaderSid, $Writable) New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory,$ErrorAction) [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
    function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel,$ErrorAction)
        if ($UserId -ne 'EXAMPLE\user_example') { throw 'Scheduler must use the resolved account name.' }
        if ($LogonType -ne 'Interactive' -or $RunLevel -ne 'Limited') { throw 'Incorrect scheduler identity policy.' }
        if ($script:badPrincipal) { return [pscustomobject]@{UserId=''} }
        [pscustomobject]@{UserId=$UserId} }
    function New-ScheduledTaskSettingsSet { param($ExecutionTimeLimit,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries,$ErrorAction) [pscustomobject]@{} }
    function New-ScheduledTask {param($Action,$Principal,$Settings,$Description,$ErrorAction) [pscustomobject]@{Principal=$Principal;Actions=@($Action);Description=$Description} }
    function Register-ScheduledTask { param($TaskName,$InputObject,$ErrorAction)
        if ($TaskName -notmatch '^SiwxLite-[a-f0-9]{32}$') { throw 'Bad task scope.' }
        if ($ErrorAction -ne 'Stop') { throw 'Register must explicitly stop on errors.' }
        if ($script:failRegister) { Write-Error 'synthetic UserId 0x80070057'; return }
        $script:registrations++; $script:task=[pscustomobject]@{Principal=$InputObject.Principal;Actions=$InputObject.Actions;State='Running'}
        $script:jobRoot=$InputObject.Actions[0].WorkingDirectory }
    function Start-ScheduledTask { param($TaskName,$ErrorAction)
        $script:startCalls++
        if ($script:failDispatch) { throw 'synthetic dispatch failure' }
        if ($script:backgroundReady) { return }
        $job = Get-ChildItem -LiteralPath $script:jobRoot -Directory | Where-Object Name -like 'job-*' | Select-Object -Last 1
        [IO.File]::WriteAllText((Join-Path $job.FullName 'logs/output.log'), "synthetic user worker`n")
        [IO.File]::WriteAllText((Join-Path $job.FullName 'logs/exit.txt'), "0`n") }
    function Get-ScheduledTask { param($TaskName,$ErrorAction) if(-not $script:nullTask){$script:task} }
    function Stop-ScheduledTask { param($TaskName,$ErrorAction) $script:stops++ }
    function Unregister-ScheduledTask { param($TaskName,$Confirm,$ErrorAction) $script:unregistrations++ }
    function Test-LitePort { param($Port) $false }
    function Test-LiteService { param($WebPort,$ExePath,$Desktop) $script:backgroundReady }
    try {
        $bridgeRoot = Join-Path $temp 'bridge1'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        Start-LiteUserTask $exe $desktop $bridgeRoot 6>$null
        if ($script:registrations -ne 1 -or $script:unregistrations -ne 1) { throw 'Successful task was not cleaned up.' }
        $script:failDispatch = $true
        $bridgeRoot = Join-Path $temp 'bridge2'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        Must-Fail { Start-LiteUserTask $exe $desktop $bridgeRoot 6>$null }
        if ($script:registrations -ne 2 -or $script:unregistrations -ne 2 -or $script:stops -ne 2) { throw 'Failed dispatch cleanup was not restricted to this task.' }
        $script:failDispatch = $false; $script:backgroundReady = $true
        $bridgeRoot = Join-Path $temp 'bridge3'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        Start-LiteUserTask $exe $desktop $bridgeRoot $true 6>$null
        if ($script:registrations -ne 3 -or $script:unregistrations -ne 2 -or $script:stops -ne 2) { throw 'Ready background was stopped while returning to the menu.' }
        $calls=$script:startCalls; $script:failRegister=$true
        $bridgeRoot = Join-Path $temp 'bridge4'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        $failure=''
        try { Start-LiteUserTask $exe $desktop $bridgeRoot $true 6>$null } catch { $failure=$_.Exception.Message }
        if ($failure -notmatch 'Stage=register task;.*synthetic UserId' -or $script:startCalls-ne$calls -or $script:unregistrations-ne 2) { throw 'Nonterminating registration failure caused dispatch, false success, or unrelated cleanup.' }
        $script:failRegister=$false; $script:nullTask=$true
        $bridgeRoot = Join-Path $temp 'bridge5'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        Must-Fail { Start-LiteUserTask $exe $desktop $bridgeRoot $true 6>$null }
        if ($script:startCalls-ne$calls){throw 'Missing registered task was dispatched'}
        $script:nullTask=$false; $script:badPrincipal=$true
        $bridgeRoot = Join-Path $temp 'bridge6'; New-Item -ItemType Directory -Path $bridgeRoot | Out-Null
        $before=$script:registrations
        Must-Fail { Start-LiteUserTask $exe $desktop $bridgeRoot $true 6>$null }
        if ($script:registrations-ne$before -or $script:startCalls-ne$calls){throw 'Empty principal was registered/dispatched'}
    } finally { $env:SystemRoot = $savedSystemRoot }
    Write-Output 'PASS: prior hash/session/CMD/relay gates; named Limited principal, full task definition, success/failure/background retention; nonterminating registration error and null task/empty principal stop before dispatch; EXE never executed.'
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
