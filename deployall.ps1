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
$skipPatterns = '^deployall', '^\.github/', '^docs/', '_test\.py$'
$candidates = git -C $RepoRoot diff --name-only $BaseCommit |
    Where-Object { $f = $_; -not ($skipPatterns | Where-Object { $f -match $_ }) }

if (-not $candidates) {
    Write-Host "No changed files found." -ForegroundColor Green
    exit 0
}

$toDeployList = [System.Collections.Generic.List[string]]::new()

foreach ($f in $candidates) {
    $localPath = Join-Path $RepoRoot ($f -replace '/', '\')
    if (-not (Test-Path $localPath)) {
        Write-Host "  missing  $f" -ForegroundColor Yellow
        continue
    }
    # Hash the local file with LF line endings (strip CR) so it matches Linux md5sum
    $localBytes = [System.IO.File]::ReadAllBytes($localPath) | Where-Object { $_ -ne 0x0D }
    $md5 = [System.Security.Cryptography.MD5]::Create()
    $localHash = ($md5.ComputeHash([byte[]]$localBytes) | ForEach-Object { $_.ToString("x2") }) -join ''
    $ErrorActionPreference = 'Continue'
    $remoteOut  = ssh $Device "tr -d '\r' < $RemoteBase/$f 2>/dev/null | md5sum" 2>$null
    $ErrorActionPreference = 'Stop'
    $remoteHash = ($remoteOut -split '\s+')[0]

    if ($localHash -eq $remoteHash) {
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
Write-Host "Restarting openwb2 ..."
$ErrorActionPreference = 'Continue'
ssh $Device "sudo systemctl restart openwb2 && echo 'Service restarted OK'"

# --- Post-restart health check ---
Write-Host ""
Write-Host "Waiting 45s for boot cycle ..." -ForegroundColor Cyan
ssh $Device "sleep 45"

Write-Host "Checking for errors ..."
$errorPatterns = 'Fehler im Prepare|Fehler im Process|TypeError|AttributeError|ImportError|SyntaxError|NameError|ModuleNotFoundError'
$logErrors = ssh $Device "tail -300 $RemoteBase/ramdisk/main.log | grep -E '$errorPatterns'" 2>$null
$threadErrors = ssh $Device "cat $RemoteBase/ramdisk/thread_errors.log 2>/dev/null | tail -20" 2>$null
$serviceStatus = ssh $Device "systemctl is-active openwb2" 2>$null
$ErrorActionPreference = 'Stop'

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
