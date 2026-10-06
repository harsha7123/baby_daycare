# EC2 runbook (dev GPU box)

Target: `i-0f1d70f493649a306` (`isaac-sim-ubuntu`, g7.4xlarge, us-east-1). It also hosts NVIDIA Isaac Sim, so
`bootstrap.sh` never reinstalls or upgrades a working NVIDIA driver. It never overwrites Docker's `daemon.json`
either: it only adds what is missing.

Run all commands from the repo root on the laptop. PowerShell and Git Bash versions behave the same.

## 1. Start the instance

- Console: EC2 → Instances → `isaac-sim-ubuntu` → Instance state → **Start**. Copy the **Public IPv4 address**.
  It changes on every start unless you attach an Elastic IP.
- Or with the AWS CLI: `.\deploy\ec2\ec2.ps1 start` (also `status`, `stop`).

The instance **bills while it is running** (GPU rate). EBS storage also bills while it is stopped.

## 2. One-time manual setup

- **Security group:** allow inbound **TCP 22 from your IP only** (`My IP` in the console). Do **not** open 8000.
  The API binds to the instance's `127.0.0.1:8000`, and you reach it through the SSH tunnel. Open 80/443 only if
  you use the `tls` profile (step 6).
- **Key permissions (Windows):** OpenSSH refuses a `.pem` that other accounts can read. The deploy script warns
  about this but does not change it for you. To fix it:
  ```powershell
  icacls C:\path\to\key.pem /inheritance:r
  icacls C:\path\to\key.pem /grant:r "$($env:USERNAME):(R)"
  ```
- **Users:** the API refuses to start without at least one user. Copy `configs/cloud.example.yaml` to
  `configs/cloud.yaml` on the laptop (it is gitignored). Run `.venv/Scripts/vdb new-user --name YOU --sites demo`,
  paste the `users:` entry into `configs/cloud.yaml`, and keep the printed key for logging in.

## 3. First deploy

```powershell
.\deploy\ec2\deploy.ps1 -HostName <public-ip> -KeyPath C:\path\to\key.pem
```
```bash
deploy/ec2/deploy.sh --host <public-ip> --key /c/path/to/key.pem
```

What the deploy does:

- Uploads **committed** files only (`git archive HEAD`). If they exist on the laptop, it also uploads
  `configs/cloud.yaml` and `deploy/.env`, and it lists them.
- Runs `bootstrap.sh`, which sets up Docker, the NVIDIA Container Toolkit and the docker group, and runs a GPU check.
- Installs the code into `/opt/vdb`.
- Generates `/opt/vdb/deploy/.env` with random secrets if the server doesn't have one.
- Runs `docker compose up -d --build`, then waits up to 10 minutes for `/api/health`.

The first build downloads PyTorch, so expect 10 minutes or more.

If the bootstrap installs a GPU driver, it exits and asks for a reboot. Run `sudo reboot` on the instance, then
deploy again.

Each deploy replaces `/opt/vdb`, except `deploy/.env` and `configs/*.yaml`. Data lives in Docker volumes and is
kept. `deploy/.env` is always mode 600. `configs/cloud.yaml` is mode 600 when the login user is uid 1000, which
matches the container's `vdb` user. For any other login user it is mode 640 with group gid 1000. The compose
network uses the fixed subnet 172.28.0.0/24, so that the API can trust X-Forwarded-For from caddy at 172.28.0.10
only. If that subnet clashes with another Docker network on the host, change it in `deploy/docker-compose.yml`.

## 4. Redeploy (after committing)

```powershell
.\deploy\ec2\deploy.ps1 -HostName <public-ip> -KeyPath C:\path\to\key.pem -SkipBootstrap
```
```bash
deploy/ec2/deploy.sh --host <public-ip> --key /c/path/to/key.pem --skip-bootstrap
```

## 5. View the dashboard (SSH tunnel)

```powershell
.\deploy\ec2\tunnel.ps1 -HostName <public-ip> -KeyPath C:\path\to\key.pem
```
```bash
deploy/ec2/tunnel.sh --host <public-ip> --key /c/path/to/key.pem
```

Then open <http://127.0.0.1:8000> and log in with your `vdb new-user` key. Press Ctrl+C to close the tunnel.

## 6. Logs and operations (on the instance: `ssh -i key.pem ubuntu@<ip>`)

```bash
cd /opt/vdb/deploy
docker compose ps
docker compose logs -f worker       # detection pipeline, model downloads
docker compose logs -f api
docker compose restart api worker   # after editing configs/cloud.yaml on the server
nvidia-smi                           # GPU use (shared with Isaac Sim, if it is running)
```

To serve public HTTPS later, point DNS at an Elastic IP and set `VDB_DOMAIN` in `/opt/vdb/deploy/.env`. Open
ports 80/443, then run `docker compose --profile tls up -d`.

## 7. Stop the instance when done

Use the console (Instance state → **Stop**, not Terminate) or `.\deploy\ec2\ec2.ps1 stop`. The containers use
`restart: unless-stopped`, so the stack comes back on the next start without another deploy.

## Production note

This us-east-1 box is for development only. For Indian daycares, run production in **ap-south-1 (Mumbai)**:
it gives lower latency from Indian CCTV sites and keeps children's footage and personal data in India, in line
with the DPDP Act. Use encrypted EBS, an Elastic IP with the `tls` profile, and a dedicated instance that doesn't
share Isaac Sim.
