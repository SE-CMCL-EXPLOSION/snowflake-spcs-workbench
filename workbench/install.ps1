# =============================================================================
# SPCS Workbench installer for Windows (PowerShell 5.1 or 7). Same steps as install.sh.
#
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -Config my-config.env
#   powershell -ExecutionPolicy Bypass -File .\install.ps1 -SkipImages
#
# Safe to re-run: existing objects are kept, data is never deleted.
# Needs: Snowflake CLI (snow), Docker Desktop (running), a connection whose user can use ACCOUNTADMIN.
# =============================================================================
param(
    [string]$Config = "config.env",
    [switch]$SkipImages
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = "Continue"   # native tools write progress to stderr; we check exit codes instead
Set-Location -LiteralPath $PSScriptRoot

$script:Step = 0
$Total = 8
$Log = Join-Path ([IO.Path]::GetTempPath()) ("workbench-install-{0}.log" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType File -Path $Log -Force | Out-Null
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Step($msg) { $script:Step++; Write-Host ""; Write-Host ("[{0}/{1}] {2}" -f $script:Step, $Total, $msg) -ForegroundColor Cyan }
function Info($msg) { Write-Host "      $msg" }
function Fail($msg) { Write-Host ""; Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Tail-Log($n) { Get-Content -LiteralPath $Log -Tail $n | ForEach-Object { Write-Host $_ -ForegroundColor DarkGray } }

# Run a program, append its output to the log, return its exit code
function Run-Native {
    param([string]$Exe, [string[]]$Arguments)
    $out = & $Exe @Arguments 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($out) { Add-Content -LiteralPath $Log -Value $out }
    return $code
}
# Run a program and return its stdout (stderr goes to the log)
function Capture-Native {
    param([string]$Exe, [string[]]$Arguments)
    $errFile = [IO.Path]::GetTempFileName()
    $out = & $Exe @Arguments 2>$errFile | Out-String
    $err = Get-Content -LiteralPath $errFile -Raw
    if ($err) { Add-Content -LiteralPath $Log -Value $err }
    Remove-Item -LiteralPath $errFile -ErrorAction SilentlyContinue
    return $out
}

Info "Detailed log: $Log"

# ---------------------------------------------------------------- settings
Step "Reading settings from $Config"
if (-not (Test-Path -LiteralPath $Config)) { Fail "Settings file '$Config' not found." }
# config.env is shell syntax: KEY="value"   # comment
$cfg = @{}
foreach ($line in Get-Content -LiteralPath $Config) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"') { $cfg[$Matches[1]] = $Matches[2] }
    elseif ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^\s#]*)') { $cfg[$Matches[1]] = $Matches[2] }
}
function Cfg($name) { if ($cfg.ContainsKey($name)) { return $cfg[$name].Trim() } else { return "" } }

$required = "SNOW_CONNECTION","APP_DATABASE","WAREHOUSE","OBJECT_PREFIX","OWNER_ROLE","ACCESS_ROLE_PREFIX",
            "SESSION_POOL_FAMILY","SESSION_POOL_MAX_NODES","IMAGE_TAG"
foreach ($v in $required) { if (-not (Cfg $v)) { Fail "$v is empty in $Config" } }

$IDENT = '^[A-Za-z_][A-Za-z0-9_$]*$'
$S = @{}   # resolved settings
foreach ($v in "APP_DATABASE","WAREHOUSE","OBJECT_PREFIX","OWNER_ROLE","ACCESS_ROLE_PREFIX","SESSION_POOL_FAMILY","DEFAULT_DATABASE","DEFAULT_SCHEMA") {
    $val = Cfg $v
    if ($val -and $val -notmatch $IDENT) { Fail "$v='$val' is not a valid Snowflake name (letters, digits, _ only)" }
    $S[$v] = $val.ToUpper()
}
$SnowConnection = Cfg "SNOW_CONNECTION"
$DataDatabases = @((Cfg "DATA_DATABASES") -split '\s+' | Where-Object { $_ } | ForEach-Object { $_.ToUpper() })
$UserAccessRoles = @((Cfg "USER_ACCESS_ROLES") -split '\s+' | Where-Object { $_ } | ForEach-Object { $_.ToUpper() })
foreach ($x in $DataDatabases + $UserAccessRoles) {
    if ($x -notmatch $IDENT) { Fail "'$x' (in DATA_DATABASES or USER_ACCESS_ROLES) is not a valid Snowflake name" }
}
$S["SESSION_POOL_MAX_NODES"] = Cfg "SESSION_POOL_MAX_NODES"
if ($S["SESSION_POOL_MAX_NODES"] -notmatch '^[1-9][0-9]*$') { Fail "SESSION_POOL_MAX_NODES must be a number" }
$S["IMAGE_TAG"] = Cfg "IMAGE_TAG"
if ($S["IMAGE_TAG"] -notmatch '^[A-Za-z0-9._-]+$') { Fail "IMAGE_TAG may only contain letters, digits, . _ -" }
$EgressHosts = @((Cfg "SESSION_EGRESS_HOSTS") -split '\s+' | Where-Object { $_ })
foreach ($h in $EgressHosts) { if ($h -notmatch '^[A-Za-z0-9.-]+(:[0-9]+)?$') { Fail "SESSION_EGRESS_HOSTS entry '$h' is not a host name" } }
if ($DataDatabases.Count -eq 0) { Info "Note: DATA_DATABASES is empty, so sessions can't query any data yet." }

# ---------------------------------------------------------------- tools
Step "Checking tools and the Snowflake connection"
if (-not (Get-Command snow -ErrorAction SilentlyContinue)) {
    Fail "Snowflake CLI not found. Install it (Windows installer): https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation  then open a NEW PowerShell window."
}
if (-not $SkipImages) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker not found. Install Docker Desktop: https://www.docker.com/products/docker-desktop/" }
    if ((Run-Native docker @("info")) -ne 0) { Fail "Docker is installed but not running. Start Docker Desktop, wait until it says 'Engine running', and re-run." }
}
$AsAdmin = @("-c", $SnowConnection, "--role", "ACCOUNTADMIN")   # every step runs as ACCOUNTADMIN

# SQL always goes through a temp file: Windows PowerShell 5.1 mangles quotes in native arguments
function New-SqlFile([string]$Sql) {
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("wbsql-{0}.sql" -f ([guid]::NewGuid().ToString("N")))
    [IO.File]::WriteAllText($tmp, $Sql, $Utf8NoBom)
    return $tmp
}
function Sf-Rows([string]$Sql) {   # rows as objects (call as @(Sf-Rows ...))
    $tmp = New-SqlFile $Sql
    $json = Capture-Native snow (@("sql") + $AsAdmin + @("-f", $tmp, "--format", "json", "--enable-templating", "NONE"))
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    try { $parsed = $json | ConvertFrom-Json } catch { return }
    foreach ($r in $parsed) { $r }   # PS 5.1 returns the whole array as one object; enumerate it
}
function Sf-Exec([string]$Sql) {   # exit code
    $tmp = New-SqlFile $Sql
    $code = Run-Native snow (@("sql") + $AsAdmin + @("-f", $tmp, "--enable-templating", "NONE"))
    Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue
    return $code
}
function Sf-Value([string]$Sql) {   # first row's V column
    $rows = @(Sf-Rows $Sql)
    if ($rows.Count -gt 0 -and $rows[0].PSObject.Properties["V"]) { return [string]$rows[0].V }
    return ""
}

$Me = Sf-Value "SELECT CURRENT_USER() AS V"
if (-not $Me) { Fail "Could not connect with connection '$SnowConnection' as ACCOUNTADMIN. Test it with:`n    snow connection test -c $SnowConnection`n(the user needs the ACCOUNTADMIN role; see $Log)" }
$Account = Sf-Value "SELECT CURRENT_ORGANIZATION_NAME() || '-' || CURRENT_ACCOUNT_NAME() AS V"
$FirstAdmin = Cfg "FIRST_ADMIN"
if (-not $FirstAdmin) { $FirstAdmin = $Me }
if ($FirstAdmin -notmatch '^[A-Za-z0-9_.@$-]+$') { Fail "FIRST_ADMIN '$FirstAdmin' is not a valid user name" }
# Plain names are case-insensitive (unquoted); names with . @ - need quoting and exact case
if ($FirstAdmin -match $IDENT) { $S["FIRST_ADMIN_IDENT"] = $FirstAdmin.ToUpper() } else { $S["FIRST_ADMIN_IDENT"] = '"' + $FirstAdmin + '"' }
if ((Sf-Exec ("DESCRIBE USER " + $S["FIRST_ADMIN_IDENT"])) -ne 0) {
    Fail ("FIRST_ADMIN: Snowflake user {0} not found (check SHOW USERS)" -f $S["FIRST_ADMIN_IDENT"])
}
Info "Account: $Account    connected as: $Me    first admin: $FirstAdmin"

$S["IMAGE_REPO_PATH"] = "/" + $S["APP_DATABASE"].ToLower() + "/spcs/images"
$S["SESSION_EAI"] = ""
$S["SESSION_HOSTS_SQL"] = ""
if ($EgressHosts.Count -gt 0) {
    $S["SESSION_EAI"] = $S["OBJECT_PREFIX"] + "_SESSION_EGRESS"
    $S["SESSION_HOSTS_SQL"] = (($EgressHosts | ForEach-Object { if ($_ -like "*:*") { "'$_'" } else { "'$($_):443'" } }) -join ", ")
}

# Fill <% NAME %> placeholders ourselves (avoids Windows argument-quoting issues with snow -D)
function Render-Sql([string]$Text) {
    return [regex]::Replace($Text, '<%\s*([A-Za-z_][A-Za-z0-9_]*)\s*%>', {
        param($m)
        $k = $m.Groups[1].Value
        if (-not $S.ContainsKey($k)) { throw "Unknown placeholder <% $k %>" }
        return $S[$k]
    })
}
function Run-SqlText([string]$Sql, [string]$Description, [string]$Source) {
    Info $Description
    if ((Sf-Exec $Sql) -ne 0) { Tail-Log 20; Fail "Step failed while running $Source (full output in $Log)" }
}
function Run-SqlFile([string]$File, [string]$Description) {
    $text = [IO.File]::ReadAllText((Join-Path $PSScriptRoot $File))
    Run-SqlText (Render-Sql $text) $Description $File
}

# ---------------------------------------------------------------- Snowflake objects
Step "Creating roles, warehouse, database and integrations"
Run-SqlFile "prereqs.sql" ("Owner role {0}, access roles {1}_USER/_AUDITOR/_ADMIN, {2}, {3}" -f $S["OWNER_ROLE"], $S["ACCESS_ROLE_PREFIX"], $S["WAREHOUSE"], $S["APP_DATABASE"])
if ($S["SESSION_EAI"]) { Run-SqlFile "session_egress.sql" ("Session egress {0}: {1}" -f $S["SESSION_EAI"], ($EgressHosts -join " ")) }
$grants = New-Object System.Text.StringBuilder
foreach ($db in $DataDatabases) {
    [void]$grants.AppendLine("GRANT ALL CALLER PRIVILEGES ON DATABASE $db TO ROLE $($S['OWNER_ROLE']);")
    foreach ($kind in "SCHEMAS","TABLES","VIEWS","MATERIALIZED VIEWS","DYNAMIC TABLES","EXTERNAL TABLES","STAGES","FILE FORMATS","FUNCTIONS") {
        [void]$grants.AppendLine("GRANT ALL INHERITED CALLER PRIVILEGES ON ALL $kind IN DATABASE $db TO ROLE $($S['OWNER_ROLE']);")
    }
}
foreach ($r in $UserAccessRoles) { [void]$grants.AppendLine("GRANT ROLE $($S['ACCESS_ROLE_PREFIX'])_USER TO ROLE $r;") }
if ($grants.Length -gt 0) {
    $dbText = if ($DataDatabases.Count) { $DataDatabases -join " " } else { "(none)" }
    $roleText = if ($UserAccessRoles.Count) { $UserAccessRoles -join " " } else { "(none)" }
    Run-SqlText ("USE ROLE ACCOUNTADMIN;`n" + $grants.ToString()) "Data access for: $dbText; Workbench users via roles: $roleText" "data access grants"
}

Step "Creating the app schema, compute pools, tables and audit log"
Run-SqlFile "setup.sql" ("Schema {0}.WORKBENCH, pools {1}_PORTAL_POOL / {1}_SESSION_POOL" -f $S["APP_DATABASE"], $S["OBJECT_PREFIX"])
Run-SqlFile "audit.sql" "Audit views SESSION_QUERY_HISTORY / SESSION_OBJECT_ACCESS"
Run-SqlFile "audit_retention.sql" "Permanent query log SESSION_QUERY_LOG + hourly task"

# ---------------------------------------------------------------- images
Step "Building and uploading container images (first time: 1-2 hours)"
$Tag = $S["IMAGE_TAG"]
if ($SkipImages) {
    Info "Skipped (-SkipImages): using existing images tagged $Tag"
} else {
    $Registry = (Capture-Native snow (@("spcs", "image-registry", "url") + $AsAdmin)).Trim()
    if (-not $Registry) { Fail "Could not get the image registry URL (see $Log)" }
    $Repo = $Registry + $S["IMAGE_REPO_PATH"]
    function Registry-Login { return (Run-Native snow (@("spcs", "image-registry", "login") + $AsAdmin)) }
    if ((Registry-Login) -ne 0) { Fail "Docker could not log in to the Snowflake image registry (see $Log)" }

    function Build-Push([string]$Ref, [string]$Context, [string]$Dockerfile, [string[]]$Extra = @()) {
        $name = $Ref.Substring($Ref.LastIndexOf("/") + 1)
        $t0 = Get-Date
        Info "building $name ..."
        $code = Run-Native docker (@("build", "--platform", "linux/amd64") + $Extra + @("-t", $Ref, "-f", $Dockerfile, $Context))
        if ($code -ne 0) { Tail-Log 30; Fail "Building $name failed (full output in $Log)" }
        # The registry often times out on large layers; finished layers are kept, so retries resume
        for ($i = 1; $i -le 30; $i++) {
            if ((Run-Native docker @("push", $Ref)) -eq 0) {
                Info ("uploaded {0} ({1} min)" -f $name, [int]((Get-Date) - $t0).TotalMinutes); return
            }
            Info "upload interrupted (normal for big images), resuming ($i/30)"
            if ($i -eq 3) { Info "tip: on a slow network, set Docker to upload one layer at a time (see README: Upload keeps failing)" }
            Start-Sleep -Seconds 5
            [void](Registry-Login)
        }
        Fail "Uploading $name failed (see $Log)"
    }
    Build-Push "$Repo/wb-portal:$Tag" "portal" "portal/Dockerfile"
    Build-Push "$Repo/wb-jupyter:$Tag" "images" "images/jupyter/Dockerfile"
    foreach ($rv in "4.4.3", "4.3.3", "4.5.1") {   # keep in sync with R_VERSIONS in portal/app.py
        Build-Push "$Repo/wb-rstudio:r$rv-$Tag" "images" "images/rstudio/Dockerfile" @("--build-arg", "R_VERSION=$rv")
    }
}

# ---------------------------------------------------------------- portal
Step "Starting the portal service"
$Svc = $S["APP_DATABASE"] + ".WORKBENCH.PORTAL"
Run-SqlFile "portal.sql" "Service $Svc ($Tag)"

Step "Waiting for the portal to be ready"
$Url = ""
for ($i = 1; $i -le 60; $i++) {
    $status = Sf-Value ("SELECT PARSE_JSON(SYSTEM`$GET_SERVICE_STATUS('{0}'))[0]:status::STRING AS V" -f $Svc)
    if ($status -eq "READY") {
        $eps = @(Sf-Rows "SHOW ENDPOINTS IN SERVICE $Svc")
        $ep = @($eps | Where-Object { $_.name -eq "portal" })
        if ($ep.Count -gt 0) {
            $u = [string]$ep[0].ingress_url
            if ($u -like "*.*" -and $u -notlike "* *") { $Url = $u; break }
        }
        Info ("running; waiting for the public URL ({0}s)" -f ($i * 15))
    } else {
        $shown = if ($status) { $status } else { "starting" }
        Info ("status: {0} ({1}s)" -f $shown, ($i * 15))
        if ($status -eq "FAILED") { Fail "Portal failed to start. Logs:`n    SELECT SYSTEM`$GET_SERVICE_LOGS('$Svc', 0, 'portal', 100);" }
    }
    Start-Sleep -Seconds 15
}
if (-not $Url) { Fail "Portal didn't become ready within 15 minutes. Check:`n    SELECT SYSTEM`$GET_SERVICE_STATUS('$Svc');" }

Step "Done"
Write-Host ""
Write-Host "  Workbench portal:   https://$Url" -ForegroundColor Green
Write-Host "  First admin:        $FirstAdmin  (sign in with your normal Snowflake login)"
Write-Host ""
Write-Host "  Next:"
Write-Host "   - Open the portal, go to Admin > Users, and give people access (User / Auditor / Admin)."
Write-Host "   - Packages: approve the R / Python packages users may install."
Write-Host "   - To upgrade later: change IMAGE_TAG in $Config and run install.ps1 again."
Write-Host ""
Remove-Item -LiteralPath $Log -ErrorAction SilentlyContinue
