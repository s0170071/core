$ErrorActionPreference = 'Stop'
$Device     = "openwb@192.168.1.20"
$RemoteBase = "/var/www/html/openWB"
$BaseCommit = "2ca307c81"
$RepoRoot   = $PSScriptRoot

Write-Host "=== openWB deploy -- scanning for changes vs $BaseCommit ===" -ForegroundColor Cyan
Write-Host "  Device : $Device"
Write-Host "  Remote : $RemoteBase"
Write-Host ""

# git diff COMMIT (no HEAD) includes both committed and uncommitted working-tree changes
$skipPatterns = '^deployall', '^\.github/', '^docs/', '^packages/modules/devices/', '_test\.py$'
$candidates = git -c core.quotePath=false -C $RepoRoot diff --diff-filter=ACMRT --name-only $BaseCommit |
    Where-Object { $f = $_; -not ($skipPatterns | Where-Object { $f -match $_ }) }

if (-not $candidates) {
    Write-Host "No changed files found." -ForegroundColor Green
    exit 0
}

$toDeployList = [System.Collections.Generic.List[string]]::new()

function Get-NormalizedMd5([string]$path) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $out = [byte[]]::new($bytes.Length)
    $n = 0
    foreach ($b in $bytes) { if ($b -ne 0x0D) { $out[$n] = $b; $n++ } }
    $md5 = [System.Security.Cryptography.MD5]::Create()
    return (($md5.ComputeHash($out, 0, $n) | ForEach-Object { $_.ToString("x2") }) -join '')
}

$existing = [System.Collections.Generic.List[string]]::new()
$localHashes = @{}
foreach ($f in $candidates) {
    $localPath = Join-Path $RepoRoot ($f -replace '/', '\')
    if (-not (Test-Path $localPath)) {
        Write-Host "  missing  $f" -ForegroundColor Yellow
        continue
    }
    $localHashes[$f] = Get-NormalizedMd5 $localPath
    [void]$existing.Add($f)
}

# One ssh round-trip for every file: the remote loop reads the paths from stdin.
# Per-file ssh calls cost ~1s each in connection setup and dominated the scan.
$remoteLoop = @'
while IFS= read -r f; do
  if [ -f "$f" ]; then h=$(tr -d '\r' < "$f" | md5sum | cut -d' ' -f1); else h=absent; fi
  printf '%s %s\n' "$h" "$f"
done
'@
$remoteHashes = @{}
if ($existing.Count -gt 0) {
    $ErrorActionPreference = 'Continue'
    $remoteOut = ($existing -join "`n") | ssh $Device ('cd ' + $RemoteBase + ' || exit 1; ' + $remoteLoop)
    $ErrorActionPreference = 'Stop'
    foreach ($line in $remoteOut) {
        if ($line -match '^(\S+)\s+(.+)$') { $remoteHashes[$Matches[2]] = $Matches[1] }
    }
}

foreach ($f in $existing) {
    if ($localHashes[$f] -eq $remoteHashes[$f]) {
        Write-Host "  ok       $f" -ForegroundColor DarkGray
    } else {
        Write-Host "  CHANGED  $f" -ForegroundColor Yellow
        $toDeployList.Add($f)
    }
}

Write-Host ""

if ($toDeployList.Count -eq 0) {
    Write-Host "Nothing to deploy -- all files match." -ForegroundColor Green
    exit 0
}

Write-Host "$($toDeployList.Count) file(s) will be deployed:" -ForegroundColor Yellow
$toDeployList | ForEach-Object { Write-Host "    $_" }
Write-Host ""

$confirm = Read-Host "Deploy and restart openwb2? [y/N]"
if ($confirm -notmatch '^[yY]$') {
    Write-Host "Aborted."
    exit 0
}

Write-Host ""
$failed = @()
foreach ($f in $toDeployList) {
    $localPath = Join-Path $RepoRoot ($f -replace '/', '\')
    Write-Host "  Deploying $f ..."
    # Strip Windows CR before uploading so Linux shebang/scripts work correctly
    $content = [System.IO.File]::ReadAllText($localPath) -replace "`r`n", "`n" -replace "`r", "`n"
    $tmpFile = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($tmpFile, $content, [System.Text.UTF8Encoding]::new($false))
    scp $tmpFile "${Device}:${RemoteBase}/$f"
    Remove-Item $tmpFile
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR deploying $f" -ForegroundColor Red
        $failed += $f
    }
}

if ($failed.Count -gt 0) {
    Write-Host ""
    Write-Host "WARNING: $($failed.Count) file(s) failed to deploy:" -ForegroundColor Red
    $failed | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
    exit 1
}

Write-Host ""
Write-Host "Restarting openwb2, waiting 45s for the boot cycle ..." -ForegroundColor Cyan
$errorPatterns = 'Fehler im Prepare|Fehler im Process|TypeError|AttributeError|ImportError|SyntaxError|NameError|ModuleNotFoundError'
# Restart, wait and collect all three health signals in a single connection, each
# section delimited so the output can be split locally.
$healthScript = @"
sudo systemctl restart openwb2 && echo 'Service restarted OK'
sleep 45
echo '###STATUS'
systemctl is-active openwb2
echo '###THREAD'
tail -20 $RemoteBase/ramdisk/thread_errors.log 2>/dev/null
echo '###LOG'
tail -300 $RemoteBase/ramdisk/main.log 2>/dev/null | grep -E '$errorPatterns'
echo '###END'
"@
$ErrorActionPreference = 'Continue'
$health = ssh $Device $healthScript
$ErrorActionPreference = 'Stop'

function Get-Section([string[]]$lines, [string]$name) {
    $start = [array]::IndexOf($lines, "###$name")
    if ($start -lt 0) { return @() }
    $section = @()
    for ($i = $start + 1; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -like '###*') { break }
        if ($lines[$i].Trim()) { $section += $lines[$i] }
    }
    return $section
}

$health | Where-Object { $_ -notlike '###*' } | Select-Object -First 1 | ForEach-Object { Write-Host $_ }
$serviceStatus = (Get-Section $health 'STATUS') -join ''
$threadErrors  = Get-Section $health 'THREAD'
$logErrors     = Get-Section $health 'LOG'

$hasErrors = $false

if ($serviceStatus -ne "active") {
    Write-Host ""
    Write-Host "FAILURE: openwb2 service is not running (status: $serviceStatus)" -ForegroundColor Red
    $ErrorActionPreference = 'Continue'
    $journalOut = ssh $Device "journalctl -u openwb2 --no-pager -n 30" 2>$null
    $ErrorActionPreference = 'Stop'
    Write-Host $journalOut -ForegroundColor Red
    $hasErrors = $true
}

if ($threadErrors) {
    Write-Host ""
    Write-Host "FAILURE: thread_errors.log contains errors:" -ForegroundColor Red
    Write-Host $threadErrors -ForegroundColor Red
    $hasErrors = $true
}

if ($logErrors) {
    Write-Host ""
    Write-Host "WARNING: main.log contains errors:" -ForegroundColor Yellow
    $logErrors | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    $hasErrors = $true
}

Write-Host ""
if ($hasErrors) {
    Write-Host "DEPLOY COMPLETED WITH ERRORS -- check output above" -ForegroundColor Red
    exit 1
} else {
    Write-Host "DEPLOY OK -- clean restart confirmed" -ForegroundColor Green
    exit 0
}