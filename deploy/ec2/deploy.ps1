<#
.SYNOPSIS
  Deploys Vision Day Baby from this Windows laptop to an EC2 GPU instance over SSH (Windows PowerShell 5.1+).

.DESCRIPTION
  Ships ONLY git-tracked files at HEAD (git archive), so data/, .venv, references/, real configs and secrets stay
  on the laptop. Exceptions, uploaded only if they exist locally (both are gitignored on purpose):
    configs/cloud.yaml and deploy/.env
  On the server they are otherwise kept from the previous deploy, or created on first deploy.
  The server-side steps are the REMOTE block inside deploy.sh (one copy, shared with the bash version).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\ec2\deploy.ps1 -HostName 3.91.x.x -KeyPath C:\keys\isaac.pem

.EXAMPLE
  .\deploy\ec2\deploy.ps1 -HostName ec2-3-91-x-x.compute-1.amazonaws.com -KeyPath C:\keys\isaac.pem -SkipBootstrap
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][Alias('Host', 'IP')][string]$HostName,
    [Parameter(Mandatory = $true)][Alias('Key')][string]$KeyPath,
    [string]$User = 'ubuntu',
    [switch]$SkipBootstrap
)

$ErrorActionPreference = 'Stop'

# Host/user end up on the ssh/scp command line: allow only IPv4/DNS-style names, never a leading "-" (option
# injection such as -oProxyCommand=...). "--" before the destination below is a second guard.
foreach ($pair in @(@('HostName', $HostName), @('User', $User))) {
    if ($pair[1] -cnotmatch '^[A-Za-z0-9._-]+$' -or $pair[1].StartsWith('-')) {
        throw "Invalid -$($pair[0]) '$($pair[1])': use an IPv4 address / DNS name or user name (letters, digits, . _ -; no leading -)."
    }
}

function Invoke-Native {
    # Runs a native command and stops on a non-zero exit code.
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE)"
    }
}

foreach ($tool in @('git', 'ssh', 'scp')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool not found on PATH (install Git for Windows / enable the Windows OpenSSH client)."
    }
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (& git -C $ScriptDir rev-parse --show-toplevel)
if ($LASTEXITCODE -ne 0) { throw 'Not inside the git repository.' }
$Root = $Root.Trim()

# ---------------------------------------------------------------- (a) key file
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "Key file not found: $KeyPath"
}
$KeyPath = (Resolve-Path -LiteralPath $KeyPath).Path
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$allowed = @($me, 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators')
$others = @((Get-Acl -LiteralPath $KeyPath).Access |
    Where-Object { $_.AccessControlType -eq 'Allow' -and ($allowed -notcontains $_.IdentityReference.Value) } |
    ForEach-Object { $_.IdentityReference.Value } | Select-Object -Unique)
if ($others.Count -gt 0) {
    Write-Warning ("$KeyPath is also readable by: " + ($others -join ', ') + '. OpenSSH may refuse it.')
    Write-Host 'Restrict it to your account (not done automatically):'
    Write-Host "  icacls `"$KeyPath`" /inheritance:r"
    Write-Host "  icacls `"$KeyPath`" /grant:r `"${env:USERNAME}:(R)`""
}

$SshOpts = @('-i', $KeyPath, '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=30',
             '-o', 'ConnectTimeout=20')
$Target = "$User@$HostName"

# ---------------------------------------------------------------- (b) release bundle
$Tmp = Join-Path ([System.IO.Path]::GetTempPath()) ('vdb-deploy-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Tmp | Out-Null
$rc = 0
try {
    $rev = (& git -C $Root rev-parse --short HEAD).Trim()
    $dirty = & git -C $Root status --porcelain --untracked-files=no
    if ($dirty) {
        Write-Host "NOTE: you have uncommitted changes; only what is committed at HEAD ($rev) is deployed."
    }
    Invoke-Native git @('-C', $Root, 'archive', '--format=tar.gz', '-o', (Join-Path $Tmp 'src.tar.gz'), 'HEAD') 'git archive'
    $date = (& git -C $Root log -1 --format=%cI HEAD).Trim()

    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText((Join-Path $Tmp 'REVISION'), "$rev $date`n", $utf8)
    Copy-Item -LiteralPath (Join-Path $ScriptDir 'bootstrap.sh') -Destination (Join-Path $Tmp 'bootstrap.sh')

    # Server-side script: the REMOTE heredoc in deploy.sh, written with LF line endings.
    $lines = [System.IO.File]::ReadAllLines((Join-Path $ScriptDir 'deploy.sh'))
    $start = -1
    $end = -1
    for ($i = 0; $i -lt $lines.Length; $i++) {
        if ($start -lt 0 -and $lines[$i].Trim() -eq "cat <<'REMOTE'") { $start = $i + 1 }
        elseif ($start -ge 0 -and $lines[$i].TrimEnd() -eq 'REMOTE') { $end = $i; break }
    }
    if ($start -lt 0 -or $end -le $start) { throw 'Could not find the REMOTE block in deploy.sh' }
    $remote = ($lines[$start..($end - 1)] -join "`n") + "`n"
    [System.IO.File]::WriteAllText((Join-Path $Tmp 'remote.sh'), $remote, $utf8)

    $upload = @((Join-Path $Tmp 'src.tar.gz'), (Join-Path $Tmp 'REVISION'), (Join-Path $Tmp 'bootstrap.sh'),
                (Join-Path $Tmp 'remote.sh'))
    $extras = @()
    $localCfg = Join-Path $Root 'configs\cloud.yaml'
    $localEnv = Join-Path $Root 'deploy\.env'
    if (Test-Path -LiteralPath $localCfg -PathType Leaf) {
        Copy-Item -LiteralPath $localCfg -Destination (Join-Path $Tmp 'cloud.yaml')
        $upload += (Join-Path $Tmp 'cloud.yaml')
        $extras += 'configs/cloud.yaml'
    }
    if (Test-Path -LiteralPath $localEnv -PathType Leaf) {
        Copy-Item -LiteralPath $localEnv -Destination (Join-Path $Tmp 'env')
        $upload += (Join-Path $Tmp 'env')
        $extras += 'deploy/.env'
    }
    $sizeMb = [math]::Round((Get-Item (Join-Path $Tmp 'src.tar.gz')).Length / 1MB, 2)
    Write-Host "Bundle: git-tracked files at $rev ($sizeMb MB)"
    if ($extras.Count -gt 0) {
        Write-Host ('Extra gitignored files included: ' + ($extras -join ' '))
    }
    else {
        Write-Host 'Extra gitignored files included: none (server keeps or creates its own configs/cloud.yaml and deploy/.env)'
    }

    # ------------------------------------------------------------ (c) upload
    Write-Host "Uploading to $Target ..."
    Invoke-Native ssh ($SshOpts + @('--', $Target, 'rm -rf ~/.vdb-deploy && mkdir -m 700 ~/.vdb-deploy')) 'Preparing the staging directory'
    Invoke-Native scp (@('-q') + $SshOpts + @('--') + $upload + @("${Target}:.vdb-deploy/")) 'Upload'

    # ------------------------------------------------------------ (d)-(h) run on the server
    $skip = 0
    if ($SkipBootstrap) { $skip = 1 }
    & ssh @SshOpts '--' $Target "bash ~/.vdb-deploy/remote.sh ~/.vdb-deploy $skip"
    $rc = $LASTEXITCODE
}
finally {
    # The bundle may hold copies of secrets; always remove it.
    Remove-Item -LiteralPath $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}

if ($rc -ne 0) {
    Write-Host "Deploy stopped (exit $rc); see the messages above." -ForegroundColor Red
    exit $rc
}

# ---------------------------------------------------------------- (i) how to view it
Write-Host ''
Write-Host "Deployed $rev to $HostName. The API listens on the instance's 127.0.0.1:8000 only; view it through an SSH tunnel:" -ForegroundColor Green
Write-Host "  .\deploy\ec2\tunnel.ps1 -HostName $HostName -KeyPath `"$KeyPath`" -User $User"
Write-Host "  (or: ssh -i `"$KeyPath`" -N -L 8000:127.0.0.1:8000 -- $Target)"
Write-Host 'then open http://127.0.0.1:8000 and log in with an API key from `vdb new-user`.'
