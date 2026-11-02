#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$scriptsDirectory = Split-Path -Parent $PSScriptRoot
$consolePath = Join-Path $scriptsDirectory 'Siwx-LiteConsole.ps1'
$consoleAst = $null
foreach ($file in Get-ChildItem -LiteralPath $scriptsDirectory -Filter '*.ps1') {
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors | Out-String) }
    if ($file.FullName -eq $consolePath) { $consoleAst = $ast }
    Write-Output "PARSE OK: $($file.Name)"
}
if (-not $consoleAst) { throw 'Console AST missing.' }
# Load only the localization helper; do not execute Windows/session/download operations.
$localizationAst = $consoleAst.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'ConvertFrom-ConsoleText'
}, $true)
if (-not $localizationAst) { throw 'Localization helper missing.' }
. ([ScriptBlock]::Create($localizationAst.Extent.Text))
$choose = ConvertFrom-ConsoleText '\u8bf7\u9009\u62e9'
$expectedChoose = [string][char]0x8BF7 + [char]0x9009 + [char]0x62E9
if ($choose -ne $expectedChoose) { throw 'Chinese prompt decoding failed.' }
if (@([IO.File]::ReadAllBytes($consolePath) | Where-Object { $_ -gt 127 }).Count) {
    throw 'Entry script must stay ASCII for legacy PowerShell HTTP/local decoding.'
}
# Decode every actual menu literal, preserving interpolation in the live script.
$menuLiterals = @($consoleAst.FindAll({
    param($node)
    $node -is [Management.Automation.Language.StringConstantExpressionAst] -or
        $node -is [Management.Automation.Language.ExpandableStringExpressionAst]
}, $true) | Where-Object { $_.Value -match '(?:`n|\n)?1 = |^4 = |^6 = |^8 = ' })
if ($menuLiterals.Count -lt 3) { throw 'Expected main menu literals were not found.' }
foreach ($literal in $menuLiterals) {
    $decoded = ConvertFrom-ConsoleText $literal.Value
    if ($decoded -notmatch '[\u4e00-\u9fff]' -or $decoded -match '\\u[0-9a-f]{4}') {
        throw 'A menu failed to render Chinese.'
    }
}
Write-Output 'PASS: Chinese prompt/menu decoding, ASCII local/irm source encoding'

foreach ($name in @('ConvertFrom-SiwxLogBytes', 'Show-SiwxLogTail')) {
    $definition = $consoleAst.Find({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name
    }, $true)
    . ([ScriptBlock]::Create($definition.Extent.Text))
}
$functionAst = $consoleAst.Find({
    param($node)
    $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Show-SiwxOutput'
}, $true)
. ([ScriptBlock]::Create($functionAst.Extent.Text))
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('siwx-console-test-' + [Guid]::NewGuid().ToString('N'))
$utf8 = New-Object Text.UTF8Encoding($false)
try {
    $position = [long]0
    Show-SiwxOutput $temporary ([ref]$position)
    if ($position -ne 0) { throw 'Missing log advanced cursor.' }
    # Include non-ASCII output, complete lines and a trailing partial line.
    $prefix = [string][char]0x5FAE + [string][char]0x4FE1 + "`n"
    [IO.File]::WriteAllText($temporary, ($prefix + 'partial'), $utf8)
    $first = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($position -ne $utf8.GetByteCount($prefix) -or $first.Count -ne 1 -or
        $first[0].ToString() -ne $prefix) { throw 'UTF-8 complete-line relay failed.' }
    $repeat = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($repeat.Count -ne 0) { throw 'Partial line relayed prematurely.' }
    [IO.File]::AppendAllText($temporary, " end`n", $utf8)
    $second = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($second.Count -ne 1 -or $second[0].ToString() -ne "partial end`n" -or
        $position -ne (Get-Item -LiteralPath $temporary).Length) {
        throw 'Incremental relay duplicated or lost content.'
    }
    # A multibyte UTF-8 character may be split across reads while the worker writes.
    $bytes = $utf8.GetBytes(([string][char]0x4F60 + "`n"))
    $append = [IO.File]::Open($temporary, 'Append', 'Write', 'ReadWrite')
    try { $append.Write($bytes, 0, 1) } finally { $append.Dispose() }
    $fragment = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($fragment.Count -ne 0) { throw 'Partial UTF-8 character relayed prematurely.' }
    $append = [IO.File]::Open($temporary, 'Append', 'Write', 'ReadWrite')
    try { $append.Write($bytes, 1, ($bytes.Length - 1)) } finally { $append.Dispose() }
    $last = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($last.Count -ne 1 -or $last[0].ToString() -ne $utf8.GetString($bytes) -or
        $position -ne (Get-Item -LiteralPath $temporary).Length) {
        throw 'Split UTF-8 character corrupted.'
    }
    Write-Output 'PASS: missing log, UTF-8 output, partial lines, incremental reads, split multibyte character'

    foreach ($functionName in @('New-SiwxCmdWorker', 'New-SiwxHiddenLauncher', 'Get-SiwxBackgroundTasks', 'Test-SiwxHttp', 'Install-SiwxRelease')) {
        $definition = $consoleAst.Find({
            param($node)
            $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
        }, $true)
        if (-not $definition) { throw "Missing function: $functionName" }
        . ([ScriptBlock]::Create($definition.Extent.Text))
    }
    # Synthetic Chinese text only; no real user identifiers or source logs.
    $chinese = [string][char]0x4E2D + [char]0x6587 + [char]0x6D4B + [char]0x8BD5
    $gbk = [Text.Encoding]::GetEncoding(936)
    $legacyLine = $chinese + "`n"
    $utf8Line = 'CMD: ' + $chinese + "`n"
    $legacyBytes = $gbk.GetBytes($legacyLine)
    $mixedBytes = $utf8.GetBytes($utf8Line) + $legacyBytes
    if ([Text.Encoding]::UTF8.GetString($legacyBytes) -eq $legacyLine) { throw 'Fixture does not reproduce the encoding mismatch.' }
    if ((ConvertFrom-SiwxLogBytes $mixedBytes) -ne ($utf8Line + $legacyLine)) { throw 'Mixed UTF8/GBK output corrupted.' }
    [IO.File]::WriteAllBytes($temporary, $mixedBytes)
    $position = [long]0
    $mixedOutput = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($mixedOutput[0].ToString() -ne ($utf8Line + $legacyLine) -or $position -ne $mixedBytes.Length) {
        throw 'Mixed output text or raw byte cursor incorrect.'
    }
    if (@(Show-SiwxOutput $temporary ([ref]$position) 6>&1).Count) { throw 'Repeated log read duplicated output.' }
    # Native GBK character split across polling reads must remain unread until a full line.
    $append = [IO.File]::Open($temporary, 'Append', 'Write', 'ReadWrite')
    try { $append.Write($legacyBytes, 0, 1) } finally { $append.Dispose() }
    $originalPosition = $position
    if (@(Show-SiwxOutput $temporary ([ref]$position) 6>&1).Count -or $position -ne $originalPosition) {
        throw 'Partial GBK character was decoded or advanced the cursor.'
    }
    $append = [IO.File]::Open($temporary, 'Append', 'Write', 'ReadWrite')
    try { $append.Write($legacyBytes, 1, ($legacyBytes.Length - 1)) } finally { $append.Dispose() }
    $continued = @(Show-SiwxOutput $temporary ([ref]$position) 6>&1)
    if ($continued[0].ToString() -ne $legacyLine -or $position -ne (Get-Item $temporary).Length) {
        throw 'Split GBK character or cursor corrupted.'
    }
    $tail = @(Show-SiwxLogTail $temporary 1 6>&1)
    if ($tail.Count -ne 1 -or $tail[0].ToString() -ne $chinese) { throw 'Background GBK tail still corrupt.' }
    # A bounded tail starts mid-character/line; discard that prefix before decoding.
    $longPrefix = 'a' * 70000
    [IO.File]::WriteAllBytes($temporary, ($utf8.GetBytes($longPrefix + "`n") + $legacyBytes))
    $bounded = @(Show-SiwxLogTail $temporary 1 6>&1)
    if ($bounded.Count -ne 1 -or $bounded[0].ToString() -ne $chinese) { throw 'Bounded background tail corrupted.' }
    [IO.File]::WriteAllBytes($temporary, $utf8.GetBytes($longPrefix + "`n"))
    if (@(Show-SiwxLogTail $temporary 1 6>&1).Count) { throw 'Tail boundary created a phantom line.' }
    # Valid-UTF8 byte sequences can also represent GBK; allow a deterministic override.
    $ambiguous = [byte[]]@(0xC2, 0xA9)
    if ((ConvertFrom-SiwxLogBytes $ambiguous 'GB18030') -ne $gbk.GetString($ambiguous) -or
        (ConvertFrom-SiwxLogBytes $ambiguous 'UTF8') -ne $utf8.GetString($ambiguous)) { throw 'Explicit log encoding override failed.' }
    [IO.File]::WriteAllBytes($temporary, $gbk.GetBytes($chinese))
    $position = [long]0
    $finalLegacy = @(Show-SiwxOutput $temporary ([ref]$position) $true 6>&1)
    if ($finalLegacy[0].ToString() -ne $chinese -or $position -ne $gbk.GetByteCount($chinese)) { throw 'Final GBK line failed.' }
    # Restore the pre-existing final-line test fixture to a complete EOF position.
    Write-Output 'PASS: reproduced UTF8/GBK mismatch, mixed lines, raw byte cursor, repeated reads, split GBK, background tail, forced encodings'
    $exe = Join-Path ([IO.Path]::GetTempPath()) 'space & !literal{LOG}/siwx.exe'
    $log = $temporary + '.log'
    $result = $temporary + '.result'
    $root = $temporary + '.root'
    foreach ($arguments in @('--version',
        '--host 127.0.0.1 --port 8787 --no-open',
        '--host 127.0.0.1 --port 8787 --no-open',
        '--host 127.0.0.1 --port 8788 --no-open',
        '--host 127.0.0.1 --port 8887 --no-open')) {
        $cmd = New-SiwxCmdWorker $exe 'S-1-5-21-1-1001' $log $result $arguments $false $root
        if (-not $cmd.Contains('"' + $exe + '" ' + $arguments + ' <nul') -or
            -not $cmd.Contains('set "SIWX_ROOT=' + $root + '"') -or
            -not $cmd.Contains('cd /d "' + $root + '"') -or
            $cmd -match "(?<!`r)`n" -or -not $cmd.Contains('space & !literal{LOG}')) {
            throw 'CMD quoting, argument forwarding, runtime root, or single-pass substitution failed.'
        }
    }
    foreach ($arguments in @('--host 0.0.0.0 --port 8787 --no-open', '--version & whoami', 'serve --host 127.0.0.1', '--host 127.0.0.1', 'backup --yes',
        '--host 127.0.0.1 --port 8787 --no-open & echo bad',
        '--host 127.0.0.1 --port 8786 --no-open',
        '--host 127.0.0.1 --port 8888 --no-open')) {
        $rejected = $false
        try { $null = New-SiwxCmdWorker $exe 'S-1-5-21-1-1001' $log $result $arguments $false $root }
        catch { $rejected = $true }
        if (-not $rejected) { throw 'Unsafe command accepted.' }
    }
    foreach ($badPath in @('bad%PATH%/siwx.exe', "bad`nline/siwx.exe", 'bad"path/siwx.exe')) {
        $rejected = $false
        try { $null = New-SiwxCmdWorker $badPath 'S-1-5-21-1-1001' $log $result '--version' $false $root }
        catch { $rejected = $true }
        if (-not $rejected) { throw 'Unsafe path accepted.' }
    }
    $probe = New-SiwxCmdWorker $exe 'S-1-5-21-1-1001' $log $result 'probe' $true $root
    if ($probe.IndexOf('goto probe_success') -gt $probe.IndexOf('cd /d') -or
        $probe.IndexOf('if not "%ACTUAL_SID%"') -gt $probe.IndexOf('goto probe_success')) {
        throw 'Probe does not verify identity before using the executable.'
    }
    [IO.File]::AppendAllText($temporary, 'final-no-newline', $utf8)
    $lastLine = @(Show-SiwxOutput $temporary ([ref]$position) $true 6>&1)
    if ($lastLine.Count -ne 1 -or $lastLine[0].ToString() -ne 'final-no-newline') {
        throw 'Final partial output line was lost.'
    }
    Write-Output 'PASS: exact SIWX commands, root, quoting, injection rejection, SID preflight, final partial line'
    # Test the portable generated script logic. Actual Windows COM still needs Windows verification.
    $savedSystemRoot = $env:SystemRoot
    try {
        if (-not $env:SystemRoot) { $env:SystemRoot = 'C:\Windows' }
        $launcher = New-SiwxHiddenLauncher ($temporary + '.cmd') $log $result
    } finally { $env:SystemRoot = $savedSystemRoot }
    $launcherPath = $temporary + '.js'
    [IO.File]::WriteAllText($launcherPath, $launcher, $utf8)
    & node (Join-Path $PSScriptRoot 'Verify-HiddenLauncher.js') $launcherPath
    if ($LASTEXITCODE -ne 0) { throw 'Generated hidden launcher logic failed.' }

    $fixtureRoot = $temporary + '.dir'
    $serviceDirectory = Join-Path (Join-Path $fixtureRoot 'Temp') ('SiwxBridge-' + ('a' * 32))
    New-Item -ItemType Directory -Path $serviceDirectory -Force | Out-Null
    $serviceLog = Join-Path $serviceDirectory (('b' * 32) + '.log')
    [IO.File]::WriteAllText($serviceLog, 'synthetic log', $utf8)
    $savedSystemRoot = $env:SystemRoot
    $savedScriptHost = $scriptHost
    try {
        $env:SystemRoot = $fixtureRoot
        $scriptHost = Join-Path $fixtureRoot 'System32/wscript.exe'
        $testSid = 'S-1-5-21-1-1001'
        $validTask = [pscustomobject]@{
            TaskName = 'SiwxBackground-' + ('b' * 32); TaskPath = '\'; State = 'Running'
            Description = (@{ Kind = 'SiwxLiteOriginal344'; UserSid = $testSid; LogPath = $serviceLog; Account = 1 } | ConvertTo-Json)
            Principal = [pscustomobject]@{ UserId = $testSid }
            Actions = @([pscustomobject]@{
                Execute = $scriptHost
                Arguments = '//B //Nologo //E:JScript "' + [IO.Path]::ChangeExtension($serviceLog, 'js') + '"'
            })
        }
        $fakeTasks = @($validTask)
        foreach ($fault in @('principal', 'kind', 'path', 'name', 'action')) {
            $badTask = $validTask | ConvertTo-Json -Depth 10 | ConvertFrom-Json
            $meta = $badTask.Description | ConvertFrom-Json
            switch ($fault) {
                'principal' { $badTask.Principal.UserId = 'S-1-5-21-1-1002' }
                'kind' { $meta.Kind = 'OtherApplication' }
                'path' { $meta.LogPath = Join-Path $fixtureRoot 'outside.log' }
                'name' { $badTask.TaskName = 'SiwxBackground-' + ('c' * 32) }
                'action' { $badTask.Actions[0].Execute = 'other.exe' }
            }
            $badTask.Description = $meta | ConvertTo-Json
            $fakeTasks += $badTask
        }
        function Get-ScheduledTask { param($TaskName, $ErrorAction) $fakeTasks }
        $managed = @(Get-SiwxBackgroundTasks $testSid)
        if ($managed.Count -ne 1 -or $managed[0].Task.TaskName -ne $validTask.TaskName) {
            throw 'Background manager accepted a foreign or malformed task.'
        }
        if ($managed[0].Port -ne 8787) { throw 'Legacy task port compatibility failed.' }
        $dynamicTask = $validTask | ConvertTo-Json -Depth 10 | ConvertFrom-Json
        $meta = $dynamicTask.Description | ConvertFrom-Json
        $meta | Add-Member -NotePropertyName Port -NotePropertyValue 8789
        $dynamicTask.Description = $meta | ConvertTo-Json
        $fakeTasks = @($dynamicTask)
        $managed = @(Get-SiwxBackgroundTasks $testSid)
        if ($managed.Count -ne 1 -or $managed[0].Port -ne 8789) { throw 'Dynamic task port not restored.' }
        foreach ($invalidPort in @('8888', '8786', '8789 & echo bad', 'not a port')) {
            $meta.Port = $invalidPort
            $dynamicTask.Description = $meta | ConvertTo-Json
            if (@(Get-SiwxBackgroundTasks $testSid).Count) { throw 'Invalid task port accepted.' }
        }
        Write-Output 'PASS: legacy/dynamic task ports and malformed port rejection'
        Write-Output 'PASS: manager scopes tasks by user SID, metadata kind, private path, exact name and launcher action'
    } finally {
        $env:SystemRoot = $savedSystemRoot
        $scriptHost = $savedScriptHost
    }

    # Distribution/install gates have a dedicated portable fixture suite.
    function Invoke-WebRequest { param($Uri) $script:requestedUri = $Uri; [pscustomobject]@{ StatusCode = 200; Content = $fakeContent } }
    $fakeContent = '<title>stories-in-wx</title>'
    if (-not (Test-SiwxHttp)) { throw 'SIWX HTTP readiness not detected.' }
    if (-not (Test-SiwxHttp 8789) -or $script:requestedUri -ne 'http://127.0.0.1:8789/') { throw 'Dynamic readiness checked wrong port.' }
    $fakeContent = '<title>another service</title>'
    if (Test-SiwxHttp) { throw 'Unrelated HTTP service accepted as SIWX.' }
    function Invoke-WebRequest { throw 'Connection refused' }
    if (Test-SiwxHttp) { throw 'HTTP failure reported ready.' }
    Write-Output 'PASS: HTTP identity and connection failure readiness guards'
} finally {
    foreach ($path in @($temporary, ($temporary + '.js'), ($temporary + '.dir'))) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
    }
}
