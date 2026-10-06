#!/usr/bin/env bash
# Deploys Vision Day Baby from this laptop to an EC2 GPU instance over SSH (Git Bash, Linux or macOS).
#
#   deploy/ec2/deploy.sh --host 3.91.x.x --key ~/keys/isaac.pem [--user ubuntu] [--skip-bootstrap]
#
# Ships ONLY git-tracked files at HEAD (`git archive`), so data/, .venv, references/, real configs and secrets
# stay on the laptop. Exceptions, uploaded only if they exist locally (both are gitignored on purpose):
#   configs/cloud.yaml  and  deploy/.env
# On the server they are otherwise kept from the previous deploy, or created on first deploy.
# deploy.ps1 runs the same server-side script: it extracts the REMOTE block below, so edit it here only.
set -euo pipefail

HOST=""
KEY=""
USER_NAME="ubuntu"
SKIP_BOOTSTRAP=0

usage() {
  sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}
while [ $# -gt 0 ]; do
  case "$1" in
    -H|--host) HOST="${2:-}"; shift 2 ;;
    -k|--key) KEY="${2:-}"; shift 2 ;;
    -u|--user) USER_NAME="${2:-}"; shift 2 ;;
    -s|--skip-bootstrap) SKIP_BOOTSTRAP=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$HOST" ] && [ -n "$KEY" ] || { echo "--host and --key are required" >&2; usage 2; }
# Host/user end up on the ssh/scp command line: allow only IPv4/DNS-style names, never a leading "-" (option
# injection such as -oProxyCommand=...). "--" before the destination below is a second guard.
valid_name() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ && "$1" != -* ]]; }
valid_name "$HOST" || { echo "Invalid --host '$HOST': use an IPv4 address or DNS name (letters, digits, . _ -; no leading -)" >&2; exit 2; }
valid_name "$USER_NAME" || { echo "Invalid --user '$USER_NAME': letters, digits, . _ - only, no leading -" >&2; exit 2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
IS_WINDOWS=0
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) IS_WINDOWS=1 ;; esac

# ---------------------------------------------------------------- (a) key file
[ -f "$KEY" ] || { echo "Key file not found: $KEY" >&2; exit 1; }
if [ "$IS_WINDOWS" -eq 1 ]; then
  WINKEY="$(cygpath -w "$KEY")"
  acl="$(MSYS_NO_PATHCONV=1 icacls "$WINKEY" 2>/dev/null || true)"
  if printf '%s' "$acl" | grep -Eqi 'Everyone|BUILTIN\\Users|Authenticated Users'; then
    echo "WARNING: $KEY is readable by other accounts; OpenSSH may refuse it. Restrict it (PowerShell or cmd):"
    echo "  icacls \"$WINKEY\" /inheritance:r"
    echo "  icacls \"$WINKEY\" /grant:r \"${USERNAME:-$USER}:(R)\""
  fi
else
  mode="$(stat -c '%a' "$KEY" 2>/dev/null || stat -f '%Lp' "$KEY")"
  if [ "${mode: -2}" != "00" ]; then
    echo "WARNING: $KEY has mode $mode; ssh may refuse it. Fix with: chmod 400 \"$KEY\""
  fi
fi

SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ConnectTimeout=20)
TARGET="$USER_NAME@$HOST"

# ---------------------------------------------------------------- (b) release bundle
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT   # holds copies of secrets; always removed

REV="$(git -C "$ROOT" rev-parse --short HEAD)"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "NOTE: you have uncommitted changes; only what is committed at HEAD ($REV) is deployed."
fi
git -C "$ROOT" archive --format=tar.gz -o "$TMP/src.tar.gz" HEAD
echo "$REV $(git -C "$ROOT" log -1 --format=%cI HEAD)" > "$TMP/REVISION"
cp "$SCRIPT_DIR/bootstrap.sh" "$TMP/bootstrap.sh"

extras=()
if [ -f "$ROOT/configs/cloud.yaml" ]; then cp "$ROOT/configs/cloud.yaml" "$TMP/cloud.yaml"; extras+=("configs/cloud.yaml"); fi
if [ -f "$ROOT/deploy/.env" ]; then cp "$ROOT/deploy/.env" "$TMP/env"; extras+=("deploy/.env"); fi
echo "Bundle: git-tracked files at $REV ($(du -h "$TMP/src.tar.gz" | cut -f1))"
if [ "${#extras[@]}" -gt 0 ]; then
  echo "Extra gitignored files included: ${extras[*]}"
else
  echo "Extra gitignored files included: none (server keeps or creates its own configs/cloud.yaml and deploy/.env)"
fi

remote_script() {
cat <<'REMOTE'
#!/usr/bin/env bash
# Server side of deploy/ec2/deploy.sh / deploy.ps1. Args: <staging dir> <skip bootstrap 0|1>
set -euo pipefail
shopt -s nullglob
STAGE="$1"
SKIP_BOOTSTRAP="${2:-0}"
APP=/opt/vdb
ME="$(id -un)"
MYGROUP="$(id -gn)"
ENV_FILE="$APP/deploy/.env"
CFG="$APP/configs/cloud.yaml"
trap 'rm -rf "$STAGE"' EXIT   # staging dir may hold uploaded secrets

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nDEPLOY ERROR: %s\n' "$*" >&2; exit 1; }
env_value() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | head -n1 | sed -e 's/^["'\'']//' -e 's/["'\'']$//'; }
redact() {  # hide .env secrets in anything we print
  local pw tok hf line
  pw="$(env_value POSTGRES_PASSWORD)"; tok="$(env_value NATS_TOKEN)"; hf="$(env_value HF_TOKEN)"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ -n "$pw" ]; then line="${line//"$pw"/***}"; fi
    if [ -n "$tok" ]; then line="${line//"$tok"/***}"; fi
    if [ -n "$hf" ]; then line="${line//"$hf"/***}"; fi
    printf '%s\n' "$line"
  done
}
# Secret/credential files are written to a temp name under umask 077 and renamed into place, so no
# world-readable copy ever exists on disk. Args: <src> <dst> <mode> [group]
secure_install() {
  local src="$1" dst="$2" mode="$3" grp="${4:-$MYGROUP}" tmp
  tmp="$(dirname "$dst")/.$(basename "$dst").tmp.$$"
  ( umask 077; sudo install -m "$mode" -o "$ME" -g "$grp" "$src" "$tmp" )
  mv -f "$tmp" "$dst"
}
cfg_sum() { if [ -f "$CFG" ]; then sha256sum "$CFG" | cut -d' ' -f1; else echo none; fi; }

# ---- (d) host bootstrap
sed -i 's/\r$//' "$STAGE/bootstrap.sh"
if [ "$SKIP_BOOTSTRAP" = 1 ]; then
  step "Skipping bootstrap"
  command -v docker >/dev/null 2>&1 || die "docker is not installed; deploy once without skip-bootstrap"
  nvidia-smi || echo "WARNING: nvidia-smi failed"
else
  step "Bootstrapping host (idempotent; existing NVIDIA driver/Docker are left alone)"
  rc=0
  sudo bash "$STAGE/bootstrap.sh" "$ME" || rc=$?
  if [ "$rc" -eq 10 ]; then
    printf '\nReboot needed: run `sudo reboot` on the instance (or stop/start it), then run the deploy again.\n' >&2
    exit 10
  fi
  [ "$rc" -eq 0 ] || die "bootstrap failed (exit $rc)"
fi
# docker-group membership only applies to new logins; use sudo for this session if needed.
if docker info >/dev/null 2>&1; then DOCKER=(docker); else DOCKER=(sudo docker); fi

# ---- (c) install the release into /opt/vdb, keeping server-side config and secrets
step "Installing $(cat "$STAGE/REVISION") into $APP"
sudo install -d -o "$ME" -g "$MYGROUP" -m 0755 "$APP"
sudo chown -R "$ME:$MYGROUP" "$APP"
old_cfg="$(cfg_sum)"
NEW="$(mktemp -d "$APP/.incoming.XXXXXX")"
tar -xzf "$STAGE/src.tar.gz" -C "$NEW"
# Remove files from the previous release (directories stay, so the running containers' bind mounts stay
# valid), except deploy/.env and configs/*.yaml. Then copy the new release in.
find "$APP" -path "$NEW" -prune -o \( -type f -o -type l \) \
  ! -path "$APP/deploy/.env" ! -path "$APP/configs/*.yaml" -print0 | xargs -0 -r rm -f
cp -a "$NEW"/. "$APP"/
rm -rf "$NEW"
find "$APP" -mindepth 1 -type d -empty -delete
cp "$STAGE/REVISION" "$APP/REVISION"
# configs/cloud.yaml (may hold camera credentials) is bind-mounted read-only into containers that run as the
# image's `vdb` user. `useradd vdb` in deploy/Dockerfile makes it the image's first user: uid 1000, gid 1000.
#  - login user is uid 1000 (`ubuntu` on EC2 Ubuntu AMIs): same uid as the container user, so 600 works.
#  - otherwise: 640 with group gid 1000 (vdb's primary group inside the image), so the container reads it via
#    the group bit and no other host account can.
CONTAINER_UID=1000
if [ "$(id -u)" = "$CONTAINER_UID" ]; then
  CFG_MODE=600; CFG_GROUP="$MYGROUP"
else
  CFG_MODE=640; CFG_GROUP="$CONTAINER_UID"
fi
if [ -f "$STAGE/cloud.yaml" ]; then
  secure_install "$STAGE/cloud.yaml" "$CFG" "$CFG_MODE" "$CFG_GROUP"
  echo "configs/cloud.yaml: replaced with the copy uploaded from the laptop"
fi
if [ -f "$STAGE/env" ]; then
  sed -i 's/\r$//' "$STAGE/env"   # staging dir is mode 700
  secure_install "$STAGE/env" "$ENV_FILE" 600
  echo "deploy/.env: replaced with the copy uploaded from the laptop"
fi

# ---- (e) deploy/.env
if [ ! -f "$ENV_FILE" ]; then
  if "${DOCKER[@]}" volume inspect vdb_dbdata >/dev/null 2>&1; then
    echo "WARNING: volume vdb_dbdata already exists and keeps its OLD Postgres password; the new one will not match."
    echo "         Restore the old deploy/.env, or (destroys data) docker volume rm vdb_dbdata."
  fi
  gen() { openssl rand -base64 32 | tr -d '/+='; }
  (
    umask 077
    {
      echo "# Generated on the server by deploy on $(date -u +%Y-%m-%dT%H:%M:%SZ). Keep secret; never commit."
      echo "# Public hostname for HTTPS (only with: docker compose --profile tls up -d). Empty = SSH tunnel only."
      echo "VDB_DOMAIN="
      echo "POSTGRES_PASSWORD=$(gen)"
      echo "NATS_TOKEN=$(gen)"
      echo "# Optional Hugging Face token (faster model downloads)."
      echo "HF_TOKEN="
    } > "$STAGE/env.generated"
  )
  secure_install "$STAGE/env.generated" "$ENV_FILE" 600
  echo "deploy/.env: generated on the server with random POSTGRES_PASSWORD and NATS_TOKEN (values not shown)"
else
  secure_install "$ENV_FILE" "$ENV_FILE" 600   # re-assert owner/mode on a .env kept from an earlier deploy
fi

# ---- (f) configs/cloud.yaml
if [ ! -f "$CFG" ]; then
  secure_install "$APP/configs/cloud.example.yaml" "$CFG" "$CFG_MODE" "$CFG_GROUP"
  echo "configs/cloud.yaml: missing, so copied from configs/cloud.example.yaml. Edit it for real sites/cameras."
else
  secure_install "$CFG" "$CFG" "$CFG_MODE" "$CFG_GROUP"   # re-assert mode/group (see CONTAINER_UID above)
fi

# ---- (g) build and start
cd "$APP/deploy"
if ! grep -Eq '^[^#]*key_sha256:[[:space:]]*["'\'']?[0-9a-fA-F]{64}' "$CFG"; then
  step "Building images only: configs/cloud.yaml has no users, so the API would refuse to start"
  "${DOCKER[@]}" compose build
  cat >&2 <<'MSG'

ACTION NEEDED: add at least one staff user, then redeploy.
  1. On the laptop:  .venv/Scripts/vdb new-user --name YOUR_NAME --sites demo
     (keep the printed API key; you log in to the dashboard with it)
  2. Put the printed `users:` entry into configs/cloud.yaml on the laptop
     (copy configs/cloud.example.yaml first if it does not exist; it is gitignored).
  3. Run the deploy again with --skip-bootstrap / -SkipBootstrap: the file is uploaded automatically.
MSG
  exit 3
fi
step "docker compose up -d --build (first build downloads PyTorch; can take 10+ minutes)"
"${DOCKER[@]}" compose up -d --build 2>&1 | redact
if [ "$old_cfg" != "$(cfg_sum)" ]; then
  step "configs/cloud.yaml changed: restarting api and worker"
  "${DOCKER[@]}" compose restart api worker 2>&1 | redact
fi

# ---- (h) health check
step "Waiting for http://127.0.0.1:8000/api/health (up to 10 minutes)"
deadline=$((SECONDS + 600))
until curl -fsS --max-time 5 http://127.0.0.1:8000/api/health >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "API did not become healthy within 10 minutes." >&2
    "${DOCKER[@]}" compose ps 2>&1 | redact
    "${DOCKER[@]}" compose logs --no-color --tail 50 2>&1 | redact
    exit 1
  fi
  sleep 5
done
echo "Health: $(curl -fsS --max-time 5 http://127.0.0.1:8000/api/health)"
"${DOCKER[@]}" compose ps 2>&1 | redact
echo
echo "Worker logs (models download on first start):  cd $APP/deploy && docker compose logs -f worker"
REMOTE
}
remote_script > "$TMP/remote.sh"

# ---------------------------------------------------------------- (c) upload
echo "Uploading to $TARGET ..."
ssh "${SSH_OPTS[@]}" -- "$TARGET" 'rm -rf ~/.vdb-deploy && mkdir -m 700 ~/.vdb-deploy'
upload=("$TMP/src.tar.gz" "$TMP/REVISION" "$TMP/bootstrap.sh" "$TMP/remote.sh")
[ -f "$TMP/cloud.yaml" ] && upload+=("$TMP/cloud.yaml")
[ -f "$TMP/env" ] && upload+=("$TMP/env")
scp -q "${SSH_OPTS[@]}" -- "${upload[@]}" "$TARGET:.vdb-deploy/"

# ---------------------------------------------------------------- (d)-(h) run on the server
rc=0
ssh "${SSH_OPTS[@]}" -- "$TARGET" "bash ~/.vdb-deploy/remote.sh ~/.vdb-deploy $SKIP_BOOTSTRAP" || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "Deploy stopped (exit $rc); see the messages above." >&2
  exit "$rc"
fi

# ---------------------------------------------------------------- (i) how to view it
cat <<EOF

Deployed $REV to $HOST. The API listens on the instance's 127.0.0.1:8000 only; view it through an SSH tunnel:
  deploy/ec2/tunnel.sh --host $HOST --key "$KEY" --user $USER_NAME
  (or: ssh -i "$KEY" -N -L 8000:127.0.0.1:8000 -- $TARGET)
then open http://127.0.0.1:8000 and log in with an API key from \`vdb new-user\`.
EOF
