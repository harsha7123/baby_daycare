#!/usr/bin/env bash
# Prepares an Ubuntu 22.04/24.04 GPU host for the Vision Day Baby stack. Runs ON the instance; deploy.sh/.ps1
# upload and run it, or run it by hand:  sudo bash deploy/ec2/bootstrap.sh [login-user]
#
# Idempotent and conservative, because the host may also run NVIDIA Isaac Sim:
#   - a working NVIDIA driver is never reinstalled, upgraded or reconfigured;
#   - Docker / the NVIDIA Container Toolkit are installed only when missing (nothing is removed or upgraded);
#   - /etc/docker/daemon.json is never overwritten: the nvidia runtime is merged in by `nvidia-ctk` only when
#     absent (a timestamped backup is kept), and docker is restarted only if something changed.
#
# Exit codes: 0 ready, 10 reboot needed (driver just installed or not loaded), anything else = failure.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  exec sudo bash "$0" "$@"
fi

LOGIN_USER="${1:-${SUDO_USER:-}}"
CUDA_TEST_IMAGE="nvidia/cuda:12.8.0-base-ubuntu24.04"
DOCKER_CHANGED=0
APT_UPDATED=0

step() { printf '\n--> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf '\nBOOTSTRAP ERROR: %s\n' "$*" >&2; exit 1; }

apt_get() { DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 "$@"; }
apt_update() { if [ "$APT_UPDATED" -eq 0 ]; then apt_get update -q; APT_UPDATED=1; fi; }
# --no-upgrade: packages that are already installed are left at their current version.
apt_install() { apt_update; apt_get install -y -q --no-upgrade --no-install-recommends "$@"; }
pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }
repo_configured() { grep -rqsF "$1" /etc/apt/sources.list /etc/apt/sources.list.d/; }

[ -n "$LOGIN_USER" ] || die "pass the login user: sudo bash bootstrap.sh ubuntu"
id "$LOGIN_USER" >/dev/null 2>&1 || die "user '$LOGIN_USER' does not exist"

# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = ubuntu ] || die "expected Ubuntu, found ${PRETTY_NAME:-unknown}"
case "${VERSION_ID:-}" in
  22.04|24.04) ;;
  *) note "WARNING: tested on Ubuntu 22.04/24.04, this is ${PRETTY_NAME}; continuing." ;;
esac
CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
ARCH="$(dpkg --print-architecture)"

base_tools() {
  local missing=()
  command -v curl >/dev/null 2>&1 || missing+=(curl)
  command -v gpg >/dev/null 2>&1 || missing+=(gnupg)
  [ -f /etc/ssl/certs/ca-certificates.crt ] || missing+=(ca-certificates)
  if [ "${#missing[@]}" -gt 0 ]; then apt_install "${missing[@]}"; fi
}

# ---------------------------------------------------------------- 1. NVIDIA driver
step "NVIDIA driver"
if command -v nvidia-smi >/dev/null 2>&1; then
  if nvidia-smi; then
    note "Driver present; leaving it untouched."
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | sed 's/^/    GPU: /'
  else
    # Typical cause: kernel updated but the instance not rebooted. Reinstalling could break Isaac Sim.
    printf '\nnvidia-smi is installed but cannot talk to the driver. Reboot the instance (sudo reboot) and\n' >&2
    printf 'run this again. The existing driver is NOT reinstalled automatically.\n' >&2
    exit 10
  fi
elif modinfo nvidia >/dev/null 2>&1 || [ -e /proc/driver/nvidia/version ]; then
  die "an NVIDIA kernel module exists but nvidia-smi is missing; not touching the driver. Install the matching nvidia-utils package by hand."
else
  note "No NVIDIA driver found; installing the recommended server (GPGPU) driver with ubuntu-drivers."
  apt_install ubuntu-drivers-common
  [ -n "$(ubuntu-drivers list --gpgpu 2>/dev/null)" ] || die "ubuntu-drivers found no NVIDIA GPU on this instance"
  ubuntu-drivers install --gpgpu
  # The --gpgpu (headless) driver ships without nvidia-smi; add the matching utils package.
  ver="$(dpkg-query -W -f='${Package}\n' 'nvidia-headless-no-dkms-*' 2>/dev/null \
         | sed -nE 's/^nvidia-headless-no-dkms-([0-9]+(-server)?)(-open)?$/\1/p' | head -n1 || true)"
  if [ -n "$ver" ]; then
    apt_install "nvidia-utils-$ver" || note "WARNING: could not install nvidia-utils-$ver (nvidia-smi)."
  fi
  printf '\nNVIDIA driver installed. A REBOOT IS NEEDED: run `sudo reboot`, wait ~1 minute, then re-run.\n' >&2
  exit 10
fi

# ---------------------------------------------------------------- 2. Docker Engine + compose plugin
step "Docker Engine + compose plugin"
add_docker_repo() {
  if repo_configured "download.docker.com/linux/ubuntu"; then return; fi
  base_tools
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$ARCH signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  APT_UPDATED=0
}

if docker compose version >/dev/null 2>&1; then
  note "$(docker --version); $(docker compose version)"
elif command -v docker >/dev/null 2>&1; then
  # Docker exists without compose. Add only the plugin, from the same source as the existing engine, so apt
  # never replaces an engine Isaac Sim may be using (docker-ce and Ubuntu's docker.io conflict).
  if pkg_installed docker-ce; then
    add_docker_repo
    apt_install docker-compose-plugin docker-buildx-plugin
  elif pkg_installed docker.io; then
    note "Engine is Ubuntu's docker.io; adding Ubuntu's compose/buildx packages to avoid a package conflict."
    apt_install docker-compose-v2 docker-buildx || apt_install docker-compose-v2
  else
    die "docker is installed from an unknown source (snap?) without 'docker compose'; install the compose v2 plugin by hand"
  fi
  DOCKER_CHANGED=1
else
  note "Docker not found; installing Docker Engine from Docker's official apt repository."
  add_docker_repo
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  DOCKER_CHANGED=1
fi
docker compose version >/dev/null 2>&1 || die "'docker compose' still not available"

if ! systemctl is-enabled --quiet docker 2>/dev/null; then
  systemctl enable docker >/dev/null 2>&1 && note "Enabled docker at boot (so the stack restarts with the instance)."
fi
if ! systemctl is-active --quiet docker; then
  systemctl start docker
fi

# ---------------------------------------------------------------- 3. NVIDIA Container Toolkit
step "NVIDIA Container Toolkit"
if command -v nvidia-ctk >/dev/null 2>&1; then
  note "Present: $(nvidia-ctk --version 2>/dev/null | head -n1)"
else
  note "Installing from NVIDIA's official apt repository."
  base_tools
  if ! repo_configured "nvidia.github.io/libnvidia-container"; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
      | gpg --batch --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
      | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
      > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    APT_UPDATED=0
  fi
  apt_install nvidia-container-toolkit
  DOCKER_CHANGED=1
fi

runtimes="$(docker info --format '{{json .Runtimes}}' 2>/dev/null || true)"
if [[ "$runtimes" == *'"nvidia"'* ]]; then
  note "Docker already has the nvidia runtime; daemon.json left as is."
else
  if [ -f /etc/docker/daemon.json ]; then
    backup="/etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)"
    cp -p /etc/docker/daemon.json "$backup"
    note "Backed up daemon.json to $backup"
  fi
  nvidia-ctk runtime configure --runtime=docker   # merges into existing daemon.json
  DOCKER_CHANGED=1
fi

if [ "$DOCKER_CHANGED" -eq 1 ]; then
  note "Restarting docker to apply changes (running containers with a restart policy come back)."
  systemctl restart docker
else
  note "No docker changes; not restarting docker."
fi

# ---------------------------------------------------------------- 4. docker group
step "docker group for $LOGIN_USER"
getent group docker >/dev/null || groupadd docker
if [[ " $(id -nG "$LOGIN_USER") " == *" docker "* ]]; then
  note "$LOGIN_USER is already in the docker group."
else
  usermod -aG docker "$LOGIN_USER"
  note "Added $LOGIN_USER to the docker group (takes effect on the next SSH login)."
fi

# ---------------------------------------------------------------- 5. GPU inside a container
step "GPU check inside a container ($CUDA_TEST_IMAGE)"
rc=0
out="$(docker run --rm --gpus all "$CUDA_TEST_IMAGE" nvidia-smi 2>&1)" || rc=$?
printf '%s\n' "$out"
if [ "$rc" -ne 0 ]; then
  if printf '%s' "$out" | grep -q 'unsatisfied condition: cuda'; then
    note "The host driver is older than CUDA 12.8. Not upgrading it (Isaac Sim depends on it)."
    note "Re-checking GPU passthrough with the CUDA version requirement disabled..."
    docker run --rm --gpus all -e NVIDIA_DISABLE_REQUIRE=true "$CUDA_TEST_IMAGE" nvidia-smi \
      || die "GPU is not visible inside containers"
    note "WARNING: passthrough works, but PyTorch cu128 wheels may need driver >= 570 on this GPU."
  else
    die "'docker run --gpus all' failed; check 'nvidia-ctk --version' and 'journalctl -u docker'"
  fi
fi

# ---------------------------------------------------------------- 6. app directory
step "/opt/vdb"
install -d -o "$LOGIN_USER" -g "$(id -gn "$LOGIN_USER")" -m 0755 /opt/vdb
note "/opt/vdb owned by $LOGIN_USER"

printf '\nBootstrap complete.\n'
