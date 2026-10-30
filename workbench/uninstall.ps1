# =============================================================================
# SPCS Workbench uninstaller for Windows (PowerShell 5.1 or 7). Same as uninstall.sh.
#
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1           # remove the app; KEEP user files, audit log, images
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Purge    # also delete schemas WORKBENCH and SPCS
#   powershell -ExecutionPolicy Bypass -File .\uninstall.ps1 -Config my-config.env
#
# The warehouse, APP_DATABASE itself and your data databases are never dropped.
# =============================================================================
param(
    [string]$Config = "config.env",
    [switch]$Purge
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"
Set-Location -LiteralPath $PSScriptRoot
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

if (-not (Test-Path -LiteralPath $Config)) { Write-Host "Settings file '$Config' not found." -ForegroundColor Red; exit 1 }
$cfg = @{}
foreach ($line in Get-Content -LiteralPath $Config) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"') { $cfg[$Matches[1]] = $Matches[2] }
    elseif ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s#]*)') { $cfg[$Matches[1]] = $Matches[2] }
}
function Cfg($name) { if ($cfg.ContainsKey($name)) { return $cfg[$name].Trim().ToUpper() } else { return "" } }
$Conn = if ($cfg.ContainsKey("SNOW_CONNECTION")) { $cfg["SNOW_CONNECTION"].Trim() } else { "" }
$Db = Cfg "APP_DATABASE"; $Prefix = Cfg "OBJECT_PREFIX"; $Owner = Cfg "OWNER_ROLE"; $Access = Cfg "ACCESS_ROLE_PREFIX"
foreach ($v in $Db, $Prefix, $Owner, $Access) {
    if ($v -notmatch '^[A-Z_][A-Z0-9_$]*$') { Write-Host "Invalid or missing name '$v' in $Config" -ForegroundColor Red; exit 1 }
}
$AsAdmin = @("-c", $Conn, "--role", "ACCOUNTADMIN")

function Run-SqlFileText([string]$Sql, [string]$Format) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("wbsql-{0}.sql" -f ([guid]::NewGuid().ToString("N")))
    [IO.File]::WriteAllText($tmp, $Sql, $Utf8NoBom)
    $extra = @("--enable-templating", "NONE")
    if ($Format) { $extra += @("--format", $Format) }
    $out = & snow (@("sql") + $AsAdmin + @("-f", $tmp) + $extra) 2>&1 | Out-String
    $code = $LASTEXITCODE
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    return @{ Code = $code; Out = $out }
}

Write-Host "This removes the SPCS Workbench in database ${Db}:"
Write-Host "  - the portal and every RStudio/Jupyter session service (running work is lost)"
Write-Host "  - compute pools ${Prefix}_PORTAL_POOL / ${Prefix}_SESSION_POOL"
Write-Host "  - integrations ${Prefix}_PKG_EGRESS / ${Prefix}_SESSION_EGRESS"
Write-Host "  - roles $Owner, ${Access}_USER / _AUDITOR / _ADMIN"
if ($Purge) {
    Write-Host "  - PURGE: schemas $Db.WORKBENCH (user files, audit log, settings) and $Db.SPCS (images)" -ForegroundColor Yellow
} else {
    Write-Host "  Kept: $Db.WORKBENCH (user files in @USER_HOMES, audit log) and $Db.SPCS (images)."
}
$answer = Read-Host "Type the database name ($Db) to continue"
if ($answer.Trim().ToUpper() -ne $Db) { Write-Host "Cancelled."; exit 1 }

# Session services are named WB_<id>; drop them before the pool
$services = @()
$r = Run-SqlFileText "SHOW SERVICES IN SCHEMA $Db.WORKBENCH;" "json"
if ($r.Code -eq 0) {
    try {
        $json = $r.Out.Substring($r.Out.IndexOf("["))
        foreach ($row in ($json | ConvertFrom-Json)) { $services += [string]$row.name }
    } catch { }
}
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("USE ROLE ACCOUNTADMIN;")
foreach ($svc in $services) { [void]$sb.AppendLine("DROP SERVICE IF EXISTS $Db.WORKBENCH.$svc;") }
if ($services.Count -gt 0) { [void]$sb.AppendLine("UPDATE $Db.WORKBENCH.SESSIONS SET STATE = 'DELETED' WHERE STATE = 'ACTIVE';") }
[void]$sb.AppendLine(@"
ALTER TASK IF EXISTS $Db.WORKBENCH.ARCHIVE_SESSION_QUERIES SUSPEND;
ALTER COMPUTE POOL IF EXISTS ${Prefix}_SESSION_POOL STOP ALL;
ALTER COMPUTE POOL IF EXISTS ${Prefix}_PORTAL_POOL STOP ALL;
DROP COMPUTE POOL IF EXISTS ${Prefix}_SESSION_POOL;
DROP COMPUTE POOL IF EXISTS ${Prefix}_PORTAL_POOL;
DROP INTEGRATION IF EXISTS ${Prefix}_PKG_EGRESS;
DROP INTEGRATION IF EXISTS ${Prefix}_SESSION_EGRESS;
DROP NETWORK RULE IF EXISTS $Db.SPCS.${Prefix}_PKG_RULE;
DROP NETWORK RULE IF EXISTS $Db.SPCS.${Prefix}_SESSION_RULE;
"@)
if ($Purge) {
    [void]$sb.AppendLine("DROP SCHEMA IF EXISTS $Db.WORKBENCH;")
    [void]$sb.AppendLine("DROP SCHEMA IF EXISTS $Db.SPCS;")
}
# Objects owned by dropped roles pass to ACCOUNTADMIN (the role running DROP ROLE)
[void]$sb.AppendLine(@"
DROP ROLE IF EXISTS ${Access}_ADMIN;
DROP ROLE IF EXISTS ${Access}_AUDITOR;
DROP ROLE IF EXISTS ${Access}_USER;
DROP ROLE IF EXISTS $Owner;
"@)

Write-Host "Removing ..."
$r = Run-SqlFileText $sb.ToString() ""
if ($r.Code -eq 0) {
    Write-Host "Done. The Workbench has been removed." -ForegroundColor Green
} else {
    Write-Host ($r.Out -split "`n" | Select-Object -Last 20 | Out-String) -ForegroundColor DarkGray
    Write-Host "Some steps failed. Re-running is safe." -ForegroundColor Red
    exit 1
}
