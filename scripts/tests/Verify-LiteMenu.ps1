$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$source=Join-Path (Split-Path -Parent $PSScriptRoot) 'Start-SiwxLite.ps1'
$ast=[Management.Automation.Language.Parser]::ParseFile($source,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
foreach($name in @('ConvertFrom-LiteText','Show-LiteMenu','Get-LiteBackgroundTasks')){
    $node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    . ([scriptblock]::Create($node.Extent.Text))
}
$Desktop=[pscustomobject]@{Sid='S-1-5-21-1-1001';Session=2}
$ListenAddress='0.0.0.0'; $Port=8787; $DataDirectory=''; $UserId=''
$script:answers=New-Object 'Collections.Generic.Queue[string]'
foreach($a in @('15','9000','6','7','0')){$script:answers.Enqueue($a)}
function Read-Host {param($Prompt) $script:answers.Dequeue()}
function Assert-LiteDistribution {param($Directory,$ManifestHash)}
function Get-LiteBackgroundTasks {param($Root,$Sid,$ExePath) @()}
$script:launches=New-Object 'Collections.Generic.List[string]'
function Select-LitePort {param($ExePath,$UserSid,$SessionId,$PreferredPort)
    if($PreferredPort-ne 9000){throw 'Menu port preference not forwarded'};9001}
function Start-LiteUserTask {param($ExePath,$Desktop,$Root,$Background)
    if($Port-ne 9001){throw 'Actual selected port not forwarded'}
    $script:launches.Add([string]$Background)}
$output=@(Show-LiteMenu 'C:\Example\siwx-lite.exe' $Desktop 'C:\Example' $true 6>&1)
if(($script:launches -join ',') -ne 'False,True'){throw 'Foreground/background menu forwarding failed'}
$text=($output|ForEach-Object ToString)-join "`n"
foreach($needle in @('6 =','7 =','8 =','9 =','0 =','9001')){if(-not $text.Contains($needle)){throw 'Menu or actual port display missing'}}
if($text -notmatch '[\u4e00-\u9fff]'){throw 'Menu did not decode Chinese'}
# Load the original metadata parser again after menu mocks.
$node=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-LiteBackgroundTasks'},$true)
. ([scriptblock]::Create($node.Extent.Text))
$saved=$env:SystemRoot; $env:SystemRoot='C:\Windows'
try{
    $root=(Get-Location).Path;$exe='C:\Example\siwx-lite.exe';$id='a'*32
    $worker=Join-Path (Join-Path $root ('job-'+$id)) 'worker.cmd'
    $log=Join-Path (Join-Path (Join-Path $root ('job-'+$id)) 'logs') 'output.log'
    $meta=@{Kind='SiwxLiteMenu341';Sid=$Desktop.Sid;Session=2;Exe=$exe;Port=8789;Bind='0.0.0.0';Log=$log}
    $valid=[pscustomobject]@{TaskName=('SiwxLite-'+$id);TaskPath='\';Description=($meta|ConvertTo-Json -Compress);State='Running';Principal=[pscustomobject]@{UserId=$Desktop.Sid};Actions=@([pscustomobject]@{Execute="$env:SystemRoot\System32\cmd.exe";Arguments=('/d /s /c ""'+$worker+'""');WorkingDirectory=$root})}
    $script:fakeTasks=@($valid)
    foreach($fault in @('sid','kind','port','path','action','exe')){
        $bad=$valid|ConvertTo-Json -Depth 10|ConvertFrom-Json
        $m=$bad.Description|ConvertFrom-Json
        switch($fault){
            sid{$bad.Principal.UserId='S-1-5-21-1-1002'}
            kind{$m.Kind='other'}
            port{$m.Port=0}
            path{$m.Log='C:\outside.log'}
            action{$bad.Actions[0].Arguments+=' --bad'}
            exe{$m.Exe='C:\Other\siwx-lite.exe'}
        }
        $bad.Description=$m|ConvertTo-Json -Compress
        $script:fakeTasks+= $bad
    }
    function Get-ScheduledTask {param($TaskName,$ErrorAction) $script:fakeTasks}
    function Assert-LitePlainPath {param($Path)}
    $found=@(Get-LiteBackgroundTasks $root $Desktop.Sid $exe)
    if($found.Count-ne 1-or $found[0].Port-ne 8789){throw 'Foreign/malformed task accepted'}
}finally{$env:SystemRoot=$saved}
Write-Output 'PASS: Chinese numbered menu, foreground/background forwarding, preferred-to-actual port and malformed/foreign task metadata rejection; no real processes/tasks.'
