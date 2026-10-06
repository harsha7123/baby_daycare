# Vision Day Baby

Real-time safety monitoring for daycare centres from CCTV video.

## Alerts (v1)

| Alert | Trigger (defaults, all configurable) |
|---|---|
| `no_adult` | Children visible, no adult in the room for 20 s |
| `ratio_breach` | More children per adult than the room allows, for 60 s |
| `phone_use` | A caretaker holding a phone for 10 s |
| `restricted_zone` | A child inside a marked zone (kitchen, door…) for 3 s |
| `camera_offline` | No video from a camera for 30 s |

Every alert gets a snapshot right away, then an H.264 clip that starts just before the condition began.
Staff mark each alert **Confirm** or **False alarm**, and those reviews give the live precision figure.

Not in v1: **abuse / aggression detection**. It needs labelled footage to train a pose/action model;
there's no reliable off-the-shelf model for it.

## How it fits together

```
video sources ──► Worker (one per GPU, all sites batched together)
                   decode @5fps ─► RF-DETR (person + phone) ─► ByteTrack ─► CLIP adult/child (per track, every 2 s)
                   ─► phone→person matching, zones ─► RulesEngine (smoothed room counts) ─► alerts / stats / clips
                                │  NATS JetStream (durable) — or in-process bus locally
                                ▼
                   API (FastAPI) ─► Postgres/SQLite ─► dashboard, WebSocket push, daily report, access log
```

Efficiency choices: newest-frame-only reading (no lag build-up), frames from every camera and site share
GPU batches, fp16 on GPU, adult/child classification once per track every 2 s instead of every frame,
and the rules engine runs in the same process (no extra network hop).

## Run locally

```bash
python -m venv .venv
.venv/Scripts/pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
.venv/Scripts/pip install -e ".[ml,dev]"
cp configs/local.example.yaml configs/local.yaml    # point `source` at a video file
.venv/Scripts/vdb --config configs/local.yaml local # prints a temporary admin key; open http://127.0.0.1:8000
```

`configs/smoke.yaml` runs two simulated cameras on `data/videos/people-walking.mp4` (supervision's sample clip).

## Security and privacy (children's footage)

- **Per-person API keys**: `vdb new-user --name Asha --sites demo` prints a key once; only its SHA-256 goes in
  the config. Each key only sees its own sites. Reviews are signed with the key owner's name.
- **Access log**: every snapshot, clip and report view is recorded (`GET /api/access-log`).
- **Retention**: snapshots and clips are deleted after 30 days, and records after 365 days. Confirmed incidents are kept.
- **Per-camera switch**: set `enabled: false` where consent is withdrawn; that camera is never read.
- **Camera credentials**: use `source: env:VAR`, so stream URLs live in `.env`, not in config files.
- **Hardening**: TLS through Caddy, NATS token auth, `no-store` / `nosniff` / strict CSP headers, public API docs off,
  and rate limiting on repeated wrong keys.
- **Still needed before a pilot**: legal review of consent under India's DPDP Act, encrypted EBS volumes, and
  OIDC staff login.

## Measuring the 90% target

"90%" means **≥ 90% precision and ≥ 90% recall for each alert type**:

1. **Offline:** label incidents in recorded videos, then run
   ```bash
   .venv/Scripts/vdb --config configs/eval.yaml eval --labels labels.json --target 0.9 --min-events 20
   ```
   The output, per alert type:
   - TP, FP, duplicate and FN counts.
   - Precision and recall, each with a 95% lower bound.
   - False alarms per camera-hour.
   - Processing speed (× real time).

   An alert type with fewer than `--min-events` labelled incidents **fails**, because a "100%" from 3 events proves
   nothing (about 35 clean events are needed before the lower bound reaches 90%). The command exits non-zero on
   failure, so it can gate CI.
2. **Live:** `GET /api/accuracy?site_id=…` gives precision from staff reviews.

`labels.json` format (times in seconds of video; label each incident from when it starts, e.g. when the last adult left):
```json
{"events": [{"room_id": "toddlers", "type": "no_adult", "start": 12.0, "end": 45.0}]}
```

**Where accuracy stands today:** the pipeline runs end to end on GPU. On a sample clip with only adults, though,
zero-shot CLIP labelled about 15% of detections as "child", which caused a false restricted-zone alert. Reaching 90% needs
**labelled daycare footage**, first to fine-tune RF-DETR on adult / child / phone (see Roadmap).

## Cloud deploy (EC2 GPU)

```bash
cd deploy && cp .env.example .env   # set domain + secrets
cp ../configs/cloud.example.yaml ../configs/cloud.yaml   # add users from `vdb new-user`
docker compose up -d --build
```

## Tests

```bash
.venv/Scripts/python -m pytest
```

## Agents

`.claude/agents/` has 26 specialist agents from [agency-agents](https://github.com/msitarzewski/agency-agents)
(MIT): engineering, security, testing, model QA, privacy, product. Ask for one by name, e.g. "use the Reality
Checker agent to review this". The full roster of 299 is in `references/00-agents/agency-agents`.

## Roadmap

1. **Data:** collect consented footage at a pilot centre. Auto-label with GroundingDINO / autodistill, then correct
   by hand. Fine-tune RF-DETR on adult / child / phone (3–5k frames, 5+ sites) to replace zero-shot CLIP. Add a second
   phone-detection pass on adult upper-body crops.
2. **CCTV → cloud link:** see [docs/cctv-to-cloud-plan.md](docs/cctv-to-cloud-plan.md). It covers the edge box,
   MediaMTX, SRT, bandwidth, fleet operations and the latency budget.
3. Pose + action model for aggression, verified by a video-language model and always reviewed by a person.
4. OIDC staff login, S3 clip storage, push notifications, TensorRT.
