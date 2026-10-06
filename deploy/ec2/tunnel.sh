#!/usr/bin/env bash
# Opens an SSH tunnel so the dashboard on the EC2 instance is reachable at http://127.0.0.1:8000 on this laptop.
#
#   deploy/ec2/tunnel.sh --host 3.91.x.x --key ~/keys/isaac.pem [--user ubuntu] [--local-port 8000]
#
# Forwards laptop 127.0.0.1:<local-port> to the instance's 127.0.0.1:8000. Port 8000 is never opened to the
# internet; only SSH (22) is needed in the security group. Press Ctrl+C to close the tunnel.
set -euo pipefail

HOST=""
KEY=""
USER_NAME="ubuntu"
LOCAL_PORT=8000
usage() { sed -n '4p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
while [ $# -gt 0 ]; do
  case "$1" in
    -H|--host) HOST="${2:-}"; shift 2 ;;
    -k|--key) KEY="${2:-}"; shift 2 ;;
    -u|--user) USER_NAME="${2:-}"; shift 2 ;;
    -p|--local-port) LOCAL_PORT="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$HOST" ] && [ -n "$KEY" ] || { echo "--host and --key are required" >&2; usage 2; }
# Host/user go on the ssh command line: IPv4/DNS-style names only, never a leading "-" (option injection).
valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ && "$1" != -* ]]; }
valid_name "$HOST" || { echo "Invalid --host '$HOST': use an IPv4 address or DNS name (no leading -)" >&2; exit 2; }
valid_name "$USER_NAME" || { echo "Invalid --user '$USER_NAME': letters, digits, . _ - only, no leading -" >&2; exit 2; }
[[ "$LOCAL_PORT" =~ ^[0-9]{1,5}$ ]] || { echo "Invalid --local-port '$LOCAL_PORT'" >&2; exit 2; }
[ -f "$KEY" ] || { echo "Key file not found: $KEY" >&2; exit 1; }

echo "Tunnel: http://127.0.0.1:$LOCAL_PORT  ->  $HOST 127.0.0.1:8000"
echo "Open that URL in your browser; press Ctrl+C to close the tunnel."
exec ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 \
  -N -L "127.0.0.1:$LOCAL_PORT:127.0.0.1:8000" -- "$USER_NAME@$HOST"
