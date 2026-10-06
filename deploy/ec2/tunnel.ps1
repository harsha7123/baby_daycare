<#
.SYNOPSIS
  Opens an SSH tunnel so the dashboard on the EC2 instance is reachable at http://127.0.0.1:8000 on this laptop.

.DESCRIPTION
  Forwards laptop 127.0.0.1:<LocalPort> to the instance's 127.0.0.1:8000. Port 8000 is never opened to the
  internet; only SSH (22) is needed in the security group. Press Ctrl+C to close the tunnel.

.EXAMPLE
  .\deploy\ec2\tunnel.ps1 -HostName 3.91.x.x -KeyPath C:\keys\isaac.pem
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][Alias('Host', 'IP')][string]$HostName,
    [Parameter(Mandatory = $true)][Alias('Key')][string]$KeyPath,
    [string]$User = 'ubuntu',
    [int]$LocalPort = 8000
)

$ErrorActionPreference = 'Stop'

# Host/user go on the ssh command line: IPv4/DNS-style names only, never a leading "-" (option injection).
foreach ($pair in @(@('HostName', $HostName), @('User', $User))) {
    if ($pair[1] -cnotmatch '^[A-Za-z0-9._-]+$' -or $pair[1].StartsWith('-')) {
        throw "Invalid -$($pair[0]) '$($pair[1])': use an IPv4 address / DNS name or user name (letters, digits, . _ -; no leading -)."
    }
}
if ($LocalPort -lt 1 -or $LocalPort -gt 65535) { throw "Invalid -LocalPort $LocalPort" }

if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) {
    throw "Key file not found: $KeyPath"
}
if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    throw 'ssh not found: enable the Windows OpenSSH client (Settings > System > Optional features).'
}

# Fail early with a clear message if something already uses the local port.
$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, $LocalPort)
try {
    $listener.Start()
    $listener.Stop()
}
catch {
    throw "Local port $LocalPort is already in use (a local vdb or another tunnel?). Use -LocalPort 8001."
}

Write-Host "Tunnel: http://127.0.0.1:$LocalPort  ->  $HostName 127.0.0.1:8000"
Write-Host 'Open that URL in your browser; press Ctrl+C here to close the tunnel.'
& ssh -i $KeyPath -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 `
    -N -L "127.0.0.1:${LocalPort}:127.0.0.1:8000" '--' "$User@$HostName"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ssh exited with code $LASTEXITCODE (instance stopped? public IP changed? port 22 allowed from your IP?)" -ForegroundColor Red
    exit $LASTEXITCODE
}
