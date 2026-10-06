<#
.SYNOPSIS
  Start, stop or check the EC2 GPU instance with the AWS CLI (optional helper).

.DESCRIPTION
  Needs the AWS CLI v2 with credentials (`aws configure`). Without it, prints the equivalent console steps.
  The public IP usually changes on every start unless the instance has an Elastic IP; this prints the new one.

.EXAMPLE
  .\deploy\ec2\ec2.ps1 start
  .\deploy\ec2\ec2.ps1 status
  .\deploy\ec2\ec2.ps1 stop
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)][ValidateSet('start', 'stop', 'status')][string]$Action,
    [string]$InstanceId = 'i-0f1d70f493649a306',
    [string]$Region = 'us-east-1',
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
$BillingNote = 'REMINDER: this GPU instance bills for every hour (per second) it is running, plus EBS storage even while stopped. Stop it when you are done: .\deploy\ec2\ec2.ps1 stop'

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Host 'The AWS CLI is not installed, so do this in the AWS console instead:' -ForegroundColor Yellow
    Write-Host "  1. Open https://console.aws.amazon.com/ec2/home?region=$Region#Instances:"
    Write-Host "  2. Select instance $InstanceId (Name: isaac-sim-ubuntu)."
    switch ($Action) {
        'start' {
            Write-Host '  3. Instance state > Start instance. Wait for "Running" and 2/2 status checks.'
            Write-Host '  4. Copy the "Public IPv4 address" (it changes on each start) for deploy/tunnel.'
            Write-Host $BillingNote -ForegroundColor Yellow
        }
        'stop' { Write-Host '  3. Instance state > Stop instance (not Terminate: that deletes it).' }
        'status' { Write-Host '  3. Read "Instance state" and "Public IPv4 address".' }
    }
    Write-Host ''
    Write-Host 'To use this script instead: install the AWS CLI v2 (winget install Amazon.AWSCLI), then run aws configure.'
    exit 0
}

function Invoke-Aws {
    param([string[]]$Arguments)
    $out = & aws @Arguments --region $Region
    if ($LASTEXITCODE -ne 0) { throw "aws $($Arguments[0..1] -join ' ') failed (exit $LASTEXITCODE)" }
    return $out
}

function Show-Status {
    $query = 'Reservations[0].Instances[0].[State.Name,InstanceType,PublicIpAddress,PublicDnsName]'
    $info = (Invoke-Aws @('ec2', 'describe-instances', '--instance-ids', $InstanceId, '--query', $query, '--output', 'text')) -split '\s+'
    Write-Host "Instance : $InstanceId ($Region)"
    Write-Host "State    : $($info[0])"
    Write-Host "Type     : $($info[1])"
    Write-Host "Public IP: $($info[2])"
    Write-Host "DNS      : $($info[3])"
    return $info
}

switch ($Action) {
    'start' {
        Invoke-Aws @('ec2', 'start-instances', '--instance-ids', $InstanceId, '--output', 'text') | Out-Null
        Write-Host "Starting $InstanceId ..."
        if (-not $NoWait) {
            Invoke-Aws @('ec2', 'wait', 'instance-running', '--instance-ids', $InstanceId) | Out-Null
        }
        $info = Show-Status
        Write-Host $BillingNote -ForegroundColor Yellow
        if ($info[2] -and $info[2] -ne 'None') {
            Write-Host ''
            Write-Host 'Next (SSH is ready ~30-60 s after "running"):'
            Write-Host "  .\deploy\ec2\deploy.ps1 -HostName $($info[2]) -KeyPath C:\path\to\key.pem"
        }
    }
    'stop' {
        Invoke-Aws @('ec2', 'stop-instances', '--instance-ids', $InstanceId, '--output', 'text') | Out-Null
        Write-Host "Stopping $InstanceId ..."
        if (-not $NoWait) {
            Invoke-Aws @('ec2', 'wait', 'instance-stopped', '--instance-ids', $InstanceId) | Out-Null
        }
        Show-Status | Out-Null
    }
    'status' {
        $info = Show-Status
        if ($info[0] -eq 'running') { Write-Host $BillingNote -ForegroundColor Yellow }
    }
}
