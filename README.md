# Vision Day Baby

Real-time safety monitoring for daycare centres from CCTV video, with an Apple-style staff dashboard.

## Alerts

| Alert | Trigger (defaults, all configurable) | Status |
|---|---|---|
| `no_adult` | Children visible, no adult in the room for 20 s | ready |
| `ratio_breach` | More children per adult than the room allows, for 60 s | ready |
| `phone_use` | A caretaker holding a phone for 10 s | ready |
| `restricted_zone` | A child inside a marked zone (kitchen, door…) for 3 s | ready |
| `camera_offline` | No video from a camera for 30 s | ready |
| `possible_aggression` | Pose-based: a fast adult hand into a child that moves the child, a child shaken back and forth while held, or a child falling right after a fast hand. A Qwen3-VL model then gives a second opinion. | **experimental, off by default** |
| `child_fall` | A child falls without help and stays down for 10 s (per room; keep off in infant/nap rooms) | **experimental, off by default** |

Every alert gets a snapshot right away, then an H.264 clip that starts just before the condition began.
Staff mark each alert **Confirm** or **False alarm**, and those reviews give the live precision figure.

**About abuse detection:** no model can reliably detect abuse. `possible_aggression` flags moments for a person to review.
The AI second opinion (likely / unlikely / unclear) is advice only: it never hides or downgrades an alert.
Turn these alerts on in production only after `vdb eval` shows they meet the target on your own labelled footage.

## How it fits together

```
cameras ──► Worker (one per GPU, every camera/site batched together)
             decode @5fps ─► RF-DETR person+phone (TensorRT fp16) ─► ByteTrack ─► CLIP adult/child per track
             ─► RF-DETR pose (only near adult–child interactions) ─► rules + behaviour analysis
             ─► alerts · room stats · evidence clips · live annotated previews ─► Qwen3-VL second opinion (async)
                          │  NATS JetStream (durable) — or in-process bus locally
                          ▼
             API (FastAPI) ─► Postgres/SQLite ─► dashboard · WebSocket push · reports · access log
```

**Latency:** the speed comes from how the models run, not from where the code lives. The vision libraries are pinned to
the exact versions cloned under `references/`, so every build is identical. The main speed-ups:
- TensorRT fp16 engines built for the exact GPU (`models.backend: tensorrt`)
- GPU-side preprocessing
- one batch across all cameras
- reading only the newest frame from each camera
- pose estimation only when an adult is near a child
- adult/child classification per person every 2 s, not every frame
- the verifier runs off the real-time path

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/pip install -e ".[ml,gpu,dev]"
```

**Demo:** synthetic data, so no camera or GPU is needed. Every item is labelled [DEMO]:
```bash
.venv/Scripts/vdb --config configs/demo.yaml demo      # prints a key; open http://127.0.0.1:8000
```

**Real pipeline on a video file:**
```bash
cp configs/local.example.yaml configs/local.yaml       # point `source` at a video file
.venv/Scripts/vdb --config configs/local.yaml local
```

## Dashboard

The dashboard has five screens: Overview (rooms, ratio rings, trends), Live (annotated camera frames), Alerts (filters, a detail
sheet with clip, AI second opinion and review), Reports (daily compliance, printable) and Activity (who viewed what, live
precision). It follows light and dark mode, works at phone width, and uses no external scripts.

## Security and privacy (children's footage)

- **Staff keys:** every person gets their own key from `vdb new-user`. Each key only sees its own sites, and reviews are signed by the key holder.
- **Access log:** snapshot, clip, report and live views are all recorded.
- **Retention:** snapshots and clips are deleted after 30 days and records after 365 days, except confirmed incidents.
- **Consent:** cameras can be switched off individually (`enabled: false`).
- **Camera credentials:** stream URLs come from environment variables (`source: env:VAR`), never config files.
- **Network:**
  - TLS through Caddy, with the real client IP passed through.
  - NATS uses token auth.
  - Strict CSP, `no-store` and `nosniff` headers.
  - API docs are off.
  - Repeated wrong keys are rate-limited.

## Measuring the 90% target

"90%" means a 95% lower confidence bound of at least 90% for both precision and recall, for each alert type, with at least
20 labelled events per type:

```bash
.venv/Scripts/vdb --config configs/eval.yaml eval --labels labels.json --target 0.9 --min-events 20
```

The output also reports false alarms per camera-hour and processing speed. Staff reviews give live precision at
`GET /api/accuracy`.

`labels.json` (times in seconds of video): `{"events": [{"room_id": "toddlers", "type": "no_adult", "start": 12, "end": 45}]}`

## Cloud deploy (EC2)

See [deploy/ec2/README.md](deploy/ec2/README.md). In short:

```powershell
.\deploy\ec2\ec2.ps1 start
.\deploy\ec2\deploy.ps1 -HostName <public-ip> -KeyPath C:\path\to\key.pem
.\deploy\ec2\tunnel.ps1 -HostName <public-ip> -KeyPath C:\path\to\key.pem   # dashboard at http://127.0.0.1:8000
```

Until real CCTV is connected, the worker loops a sample clip. The plan for the CCTV link is in
[docs/cctv-to-cloud-plan.md](docs/cctv-to-cloud-plan.md).

## Tests

```bash
.venv/Scripts/python -m pytest      # 100 tests
```

## Agents

`.claude/agents/` has 26 specialist agents from [agency-agents](https://github.com/msitarzewski/agency-agents) (MIT):
Reality Checker, Model QA, the security auditors, Frontend Developer, DevOps Automator and others.

## Roadmap

1. **Pilot data:** collect consented footage, then auto-label and correct it. Fine-tune RF-DETR on adult/child/phone, which replaces CLIP.
2. **Calibrate the experimental alerts:** cache per-frame results, sweep the behaviour thresholds offline on staged and normal footage, and choose operating points on a held-out site.
3. Run pose at the camera's native fps during interactions, to catch fast blows.
4. **CCTV → cloud:** an edge box per centre with MediaMTX and SRT (see the plan).
5. OIDC staff login, S3 clip storage, push notifications.
